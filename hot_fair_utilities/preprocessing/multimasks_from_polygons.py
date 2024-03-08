# Patched from ramp-code.scripts.multi_masks_from_polygons created for ramp project by carolyn.johnston@dev.global

from pathlib import Path
from tqdm import tqdm
from ramp.data_mgmt.chip_label_pairs import get_tq_chip_label_pairs, construct_mask_filepath
from solaris.vector.mask import crs_is_metric
from solaris.utils.geo import get_crs
from solaris.utils.core import _check_rasterio_im_load
from ramp.utils.multimask_utils import df_to_px_mask, multimask_to_sparse_multimask
import rasterio as rio
from ramp.utils.img_utils import to_channels_first
import geopandas as gpd


def get_rasterio_shape_and_transform(image_path):
    # get the image shape and the affine transform to pass into df_to_px_mask.
    with rio.open(image_path) as rio_dset:
        shape = rio_dset.shape
        transform = rio_dset.transform
    return shape, transform


def multimasks_from_polygons(in_poly_dir, in_chip_dir, out_mask_dir, input_contact_spacing=4, input_boundary_width=2):
    """
    Create multichannel building footprint masks from a folder of geojson files.
    This also requires the path to the matching image chips directory.

    Args:
        in_poly_dir (str): Path to directory containing geojson files.
        in_chip_dir (str): Path to directory containing image chip files with names matching geojson files.
        out_mask_dir (str): Path to directory containing output SDT masks.
        input_contact_spacing (int, optional): Width in pixel units of boundary class around building footprints,
            in pixels.
        input_boundary_width (int, optional): Pixels that are closer to two different polygons than contact_spacing
            (in pixel units) will be labeled with the contact mask.

    Example:
        multimasks_from_polygons(
            "data/preprocessed/labels",
            "data/preprocessed/chips",
            "data/preprocessed/multimasks"
        )
    """

    # If output mask directory doesn't exist, try to create it.
    Path(out_mask_dir).mkdir(parents=True, exist_ok=True)

    chip_label_pairs = get_tq_chip_label_pairs(in_chip_dir, in_poly_dir)

    chip_paths, label_paths = list(zip(*chip_label_pairs))

    # construct the output mask file names from the chip file names.
    # these will have the same base filenames as the chip files,
    # with a mask.tif extension in place of the .tif extension.
    mask_paths = [construct_mask_filepath(out_mask_dir, chip_path) for chip_path in chip_paths]

    # construct a list of full paths to the mask files
    json_chip_mask_zips = zip(label_paths, chip_paths, mask_paths)

    for json_path, chip_path, mask_path in tqdm(json_chip_mask_zips, desc="Multimasks for input"):

        # We will run this on very large directories, and some label files might fail to process.
        # We want to be able to resume mask creation from where we left off.
        if Path(mask_path).is_file():
            continue

        # workaround for bug in solaris
        mask_shape, mask_transform = get_rasterio_shape_and_transform(chip_path)

        gdf = gpd.read_file(json_path)

        # remove empty and null geometries
        gdf = gdf[~gdf["geometry"].isna()]
        gdf = gdf[~gdf.is_empty]

        reference_im = _check_rasterio_im_load(chip_path)

        if get_crs(gdf) != get_crs(reference_im):
            # BUGFIX: if crs's don't match, reproject the geodataframe
            gdf = gdf.to_crs(get_crs(reference_im))

        if crs_is_metric(gdf):
            meters = True

            # CJ 20220824: convert pixels to meters for call to df_to_pix_mask
            boundary_width = min(reference_im.res) * input_boundary_width
            contact_spacing = min(reference_im.res) * input_contact_spacing
        else:
            meters = False
            boundary_width = input_boundary_width
            contact_spacing = input_contact_spacing

        # NOTE: solaris does not support multipolygon geodataframes
        # So first we call explode() to turn multipolygons into polygon dataframes
        # ignore_index=True prevents polygons from the same multipolygon from being grouped into a series. -+
        gdf_poly = gdf.explode(ignore_index=True)

        # multi_mask is a one-hot, channels-last encoded mask
        onehot_multi_mask = df_to_px_mask(df=gdf_poly,
                                          out_file=mask_path,
                                          shape=mask_shape,
                                          do_transform=True,
                                          affine_obj=None,
                                          channels=['footprint', 'boundary', 'contact'],
                                          reference_im=reference_im,
                                          boundary_width=boundary_width,
                                          contact_spacing=contact_spacing,
                                          out_type="uint8",
                                          meters=meters)

        # convert onehot_multi_mask to a sparse encoded mask
        # of shape (1,H,W) for compatibility with rasterio writer
        sparse_multi_mask = multimask_to_sparse_multimask(onehot_multi_mask)
        sparse_multi_mask = to_channels_first(sparse_multi_mask)

        # write out sparse mask file with rasterio.
        with rio.open(chip_path, "r") as src:
            meta = src.meta.copy()
            meta.update(count=sparse_multi_mask.shape[0])
            meta.update(dtype='uint8')
            meta.update(nodata=None)
            with rio.open(mask_path, 'w', **meta) as dst:
                dst.write(sparse_multi_mask)
