import logging
from math import ceil
from pathlib import Path

import dask.dataframe as dd
import geopandas as gpd
import numpy as np
import xarray as xr
from multiscale_spatial_image import MultiscaleSpatialImage
from shapely.geometry import Polygon, box
from spatial_image import SpatialImage
from spatialdata import SpatialData
from spatialdata.models import ShapesModel
from spatialdata.transformations import get_transformation

from ._constants import ROI, SopaFiles, SopaKeys
from ._sdata import get_spatial_image, to_intrinsic
from .segmentation.aggregate import _tree_to_cell_id, map_transcript_to_cell

log = logging.getLogger(__name__)


class Patches1D:
    def __init__(self, xmin, xmax, patch_width, patch_overlap, int_coords):
        self.xmin, self.xmax = xmin, xmax
        self.delta = self.xmax - self.xmin

        self.patch_width = patch_width
        self.patch_overlap = patch_overlap
        self.int_coords = int_coords

        self._count = self.count()

    def count(self):
        if self.patch_width >= self.delta:
            return 1
        return ceil((self.delta - self.patch_overlap) / (self.patch_width - self.patch_overlap))

    def update(self, patch_width):
        self.patch_width = patch_width
        assert self._count == self.count()

    def tight_width(self):
        return ceil((self.delta + (self._count - 1) * self.patch_overlap) / self._count)

    def __getitem__(self, i):
        start_delta = i * (self.patch_width - self.patch_overlap)
        x0, x1 = self.xmin + start_delta, self.xmin + start_delta + self.patch_width

        return [int(x0), int(x1)] if self.int_coords else [x0, x1]


class Patches2D:
    def __init__(
        self,
        sdata: SpatialData,
        element_name: str,
        patch_width: float | int,
        patch_overlap: float | int = 50,
    ):
        self.sdata = sdata
        self.element = sdata[element_name]

        if isinstance(self.element, MultiscaleSpatialImage):
            self.element = get_spatial_image(sdata, element_name)

        if isinstance(self.element, SpatialImage) or isinstance(self.element, xr.DataArray):
            xmin, ymin = 0, 0
            xmax, ymax = len(self.element.coords["x"]), len(self.element.coords["y"])
            tight, int_coords = False, True
        elif isinstance(self.element, dd.DataFrame):
            xmin, ymin = self.element.x.min().compute(), self.element.y.min().compute()
            xmax, ymax = self.element.x.max().compute(), self.element.y.max().compute()
            tight, int_coords = True, False
        else:
            raise ValueError(f"Invalid element type: {type(self.element)}")

        self.patch_x = Patches1D(xmin, xmax, patch_width, patch_overlap, int_coords)
        self.patch_y = Patches1D(ymin, ymax, patch_width, patch_overlap, int_coords)

        self.patch_width = patch_width
        self.patch_overlap = patch_overlap
        self.tight = tight
        self.int_coords = int_coords

        self.roi = sdata.shapes[ROI.KEY] if ROI.KEY in sdata.shapes else None
        if self.roi is not None:
            self.roi = to_intrinsic(sdata, self.roi, element_name).geometry[0]

        assert self.patch_width > self.patch_overlap

        width_x = self.patch_x.tight_width()
        width_y = self.patch_y.tight_width()

        if self.tight:
            self.patch_width = max(width_x, width_y)
            self.patch_x.update(self.patch_width)
            self.patch_y.update(self.patch_width)

        self._ilocs = []

        for i in range(self.patch_x._count * self.patch_y._count):
            ix, iy = self.pair_indices(i)
            bounds = self.iloc(ix, iy)
            patch = box(*bounds)
            if self.roi is None or self.roi.intersects(patch):
                self._ilocs.append((ix, iy))

    def pair_indices(self, i: int) -> tuple[int, int]:
        iy, ix = divmod(i, self.patch_x._count)
        return ix, iy

    def iloc(self, ix: int, iy: int):
        xmin, xmax = self.patch_x[ix]
        ymin, ymax = self.patch_y[iy]
        return [xmin, ymin, xmax, ymax]

    def __getitem__(self, i) -> tuple[int, int, int, int]:
        """One patche bounding box: (xmin, ymin, xmax, ymax)"""
        if isinstance(i, slice):
            start, stop, step = i.indices(len(self))
            return [self[i] for i in range(start, stop, step)]

        return self.iloc(*self._ilocs[i])

    def __len__(self):
        return len(self._ilocs)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def polygon(self, i: int) -> Polygon:
        square = box(*self[i])
        return square if self.roi is None else square.intersection(self.roi)

    @property
    def polygons(self) -> list[Polygon]:
        return [self.polygon(i) for i in range(len(self))]

    def write(self, overwrite: bool = True):
        geo_df = gpd.GeoDataFrame(
            {"geometry": self.polygons, SopaKeys.BOUNDS: [self[i] for i in range(len(self))]}
        )
        geo_df = ShapesModel.parse(
            geo_df, transformations=get_transformation(self.element, get_all=True)
        )
        self.sdata.add_shapes(SopaKeys.PATCHES, geo_df, overwrite=overwrite)

    def patchify_transcripts(
        self,
        baysor_dir: str,
        cell_key: str = None,
        unassigned_value: int | str = None,
        use_prior: bool = False,
    ):
        import shapely
        from shapely.geometry import Point
        from tqdm import tqdm

        df = self.element

        if cell_key is not None and unassigned_value is not None:
            df[cell_key] = df[cell_key].replace(unassigned_value, 0)

        baysor_dir = Path(baysor_dir)

        prior_boundaries = self.sdata[SopaKeys.CELLPOSE_BOUNDARIES] if use_prior else None

        log.info(f"Making {len(self)} sub-CSV for Baysor")
        for i, patch in enumerate(tqdm(self.polygons)):
            patch_dir = (baysor_dir / str(i)).absolute()
            patch_dir.mkdir(parents=True, exist_ok=True)
            patch_path = patch_dir / SopaFiles.BAYSOR_TRANSCRIPTS

            tx0, ty0, tx1, ty1 = patch.bounds
            df = df[(df.x >= tx0) & (df.x <= tx1) & (df.y >= ty0) & (df.y <= ty1)]

            if patch.area < box(*patch.bounds).area:
                sub_df = df.compute()  # TODO: make it more efficient using map partitions?

                points = [Point(row) for row in sub_df[["x", "y"]].values]
                tree = shapely.STRtree(points)
                indices = tree.query(patch, predicate="intersects")
                sub_df = sub_df.iloc[indices]

                if prior_boundaries:
                    sub_df[SopaKeys.BAYSOR_CELL_KEY] = _tree_to_cell_id(
                        tree, points, prior_boundaries
                    )

                sub_df.to_csv(patch_path)
            else:
                if prior_boundaries is not None:
                    df = map_transcript_to_cell(self.sdata, SopaKeys.BAYSOR_CELL_KEY)
                df.to_csv(patch_path, single_file=True)

        log.info(f"Patches saved in directory {baysor_dir}")