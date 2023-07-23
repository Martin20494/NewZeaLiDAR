# -*- coding: utf-8 -*-
"""
This module contains utility functions for the package.
"""
import gc
import shutil
import time
import json
import shapely
import logging
import os
import pathlib
import pygeos  # for drop z
from typing import Type, TypeVar, Union
from datetime import datetime, timedelta
from collections import OrderedDict
from shapely.geometry import MultiPolygon, Polygon, GeometryCollection, box
from shapely import unary_union
import xarray as xr
import rioxarray as rxr

import geojson
import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.declarative import declarative_base

from src import tables

logger = logging.getLogger(__name__)

load_dotenv()

Base = declarative_base()

# Generic type, used for static type checking
Ttable = TypeVar("Ttable", bound=Base)
T = TypeVar("T", str, bool, int, float)

CATCHMENT_RESOLUTION = 30  # the resolution of the catchment in meters
EPS = 0.1  # epsilon for float comparison


def get_env_variable(var_name: str, default: T = None, allow_empty: bool = False, cast_to: T = str) -> T:
    """
    Reads an environment variable, with settings to allow defaults, empty values, and type casting
    To read a boolean EXAMPLE_ENV_VAR=False use get_env_variable("EXAMPLE_ENV_VAR", cast_to=bool)

    :param var_name: The name of the environment variable to retrieve.
    :param default: Default return value if the environment variable does not exist. Doesn't override empty string vars.
    :param allow_empty: If False then a KeyError will be raised if the environment variable is empty.
    :param cast_to: The type to cast to eg. str, int, or bool
    :return: The environment variable, or default if it does not exist, as type T.
    :raises: KeyError if allow_empty is False and the environment variable is empty string or None
    :raises: ValueError if cast_to is not compatible with the value stored.
    """
    env_var = os.getenv(var_name, default)
    if not allow_empty and env_var in (None, ""):
        raise KeyError(f"Environment variable {var_name} not set, and allow_empty is False")
    return _cast_str(env_var, cast_to)


def _cast_str(str_to_cast: str, cast_to: T) -> T:
    """
    Takes a string and casts it to necessary primitive builtin types. Tested with int, float, and bool.
    For bools, this detects if the value is in the case-insensitive sets {"True", "T", "1"} or {"False", "F", "0"}
    and raises a ValueError if not. For example _cast_str("False", bool) -> False

    :param str_to_cast: The string that is going to be casted to the type
    :param cast_to: The type to cast to e.g. bool
    :return: The string casted to type T defined by cast_to.
    :raises: ValueError if [cast_to] is not compatible with the value stored.
    """
    # Special cases i.e. casts that aren't of the form int("7") -> 7
    if cast_to == bool:
        # For bool we have the problem where bool("False") == True but we want this function to return False
        truth_values = {"true", "t", "1"}
        false_values = {"false", "f", "0"}
        if str_to_cast.lower() in truth_values:
            return True
        elif str_to_cast.lower() in false_values:
            return False
        raise ValueError(f"{str_to_cast} being casted to bool but is not in {truth_values} or {false_values}")
    # General case
    return cast_to(str_to_cast)


def get_database(null_pool: bool = False, pool_pre_ping: bool = False) -> Type[create_engine]:
    """
    Exit the program if connection fails.

    :param null_pool: If True, use NullPool to avoid connection pool limitation in multiprocessing. Default is False.
    :param pool_pre_ping: If True, enable pool_pre_ping to check database connection before using. Default is False.
    """
    try:
        engine = get_connection_from_profile(null_pool=null_pool, pool_pre_ping=pool_pre_ping)
        return engine
    except ConnectionAbortedError:
        raise ConnectionAbortedError("Connection to database failed. Check .env file.")


def get_connection_from_profile(null_pool: bool = False, pool_pre_ping: bool = False) -> Type[create_engine]:
    """Sets up database connection from .env file."""
    connection_keys = ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
    host, port, db, username, password = (get_env_variable(key) for key in connection_keys)
    assert any(connection_cred is None for connection_cred in [host, port, db, username, password]) is False, (
        "Error:: One or more of the connection credentials is missing."
    )
    return get_engine(db, username, host, port, password, null_pool=null_pool, pool_pre_ping=pool_pre_ping)


def get_engine(db: str, user: str, host: str, port: str, password: str,
               null_pool: bool = False,
               pool_pre_ping: bool = False) -> Type[create_engine]:
    """Get SQLalchemy engine using credentials.

    :param db: database name
    :param user: Username
    :param host: Hostname of the database server
    :param port: Port number
    :param password: Password for the database
    :param null_pool: If True, use NullPool to avoid connection pool limitation in multiprocessing. Default is False.
    :param pool_pre_ping: If True, enable pool_pre_ping to check database connection before using. Default is False.
    :return: SQLalchemy engine
    """
    url = f'postgresql://{user}:{password}@{host}:{port}/{db}'
    poolclass = NullPool if null_pool else None
    engine = create_engine(url, poolclass=poolclass, pool_pre_ping=pool_pre_ping)
    Base.metadata.create_all(engine)
    return engine


def timeit(f):
    """timer decorator"""
    def wrapper(*args, **kwargs):
        start = datetime.now()
        result = f(*args, **kwargs)
        span = datetime.now() - start
        logging.info(f"\n*** TIME IT ***\n{f.__name__} runtime: {span}\n***************")
        return result
    return wrapper


def cast_geodataframe(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    cast data type of geodataframe to correct type to avoid error when saving to database.
    the input columns must contain ['catch_id', 'area', 'geometry']
    """
    gdf['catch_id'] = gdf['catch_id'].astype(int)
    gdf['area'] = gdf['area'].astype(float)
    gdf = gdf.set_geometry('geometry')
    gdf = gdf.set_crs(epsg=2193)
    return gdf


def get_files(filetype: str, file_path: str, expect: int = -1) -> Union[list, str]:
    """ To get the path of all the files with filetype extension in the input file path. """
    file_path_list = []
    for file in pathlib.Path(file_path).rglob(f"*{filetype}"):
        file_path_list.append(str(file.as_posix()))
    if expect < 0 or 1 < expect == len(file_path_list):
        return file_path_list
    elif expect == 1 and len(file_path_list) == 1:
        return file_path_list[0]
    else:
        raise FileNotFoundError(
            f"Error:: Find {len(file_path_list)} {filetype} files in {file_path}, where expect {expect}."
        )


def drop_z(ds: gpd.GeoSeries) -> gpd.GeoSeries:
    """
    Drop Z coordinates from GeoSeries, returns GeoSeries
    Requires pygeos to be installed, otherwise it get error without warning.
    source: https://gist.github.com/rmania/8c88377a5c902dfbc134795a7af538d8
    """
    return gpd.GeoSeries.from_wkb(ds.to_wkb(output_dimension=2))


def gen_boundary_file(data_path: Union[str, pathlib.Path],
                      gdf_boundary: gpd.GeoDataFrame,
                      index: Union[int, str],
                      buffer: Union[int, float] = 0, crs: str = '2193'):
    """
    generate boundary file based on the input geodataframe.
    Save to DATA_DIR/data_path/index/index.geojson.
    """
    if buffer > 0:
        gdf_boundary['geometry'] = gdf_boundary['geometry'].buffer(buffer, join_style='mitre')
    feature_crs = {
        "type": "name",
        "properties": {
            "name": f"urn:ogc:def:crs:EPSG::{crs}"
        }
    }
    feature = geojson.Feature(geometry=gdf_boundary['geometry'][0], properties={})
    feature_collection = geojson.FeatureCollection([feature], name='selected_polygon', crs=feature_crs)
    file_path = str(pathlib.Path(data_path) /
                    pathlib.Path(f'{index}') /
                    pathlib.Path(f'{index}.geojson'))
    if not os.path.exists(os.path.dirname(file_path)):
        os.makedirs(os.path.dirname(file_path))
    with open(file_path, 'w') as f:
        geojson.dump(feature_collection, f, indent=2)
    logging.info(f"Generate region of interest geojson file at {file_path}.")


def map_dataset_name_with_id(engine, table: str, instructions_file: Union[str, pathlib.Path]) -> None:
    """Mapping dataset name with its id in the database, and save in a json file."""
    query = f"SELECT name, id FROM {table} ;"
    df = pd.read_sql(query, engine).sort_values(by='id')
    with open(instructions_file, 'r') as f:
        instructions = json.load(f)
        if instructions["instructions"].get("dataset_mapping"):
            instructions["instructions"]["dataset_mapping"] = {"lidar": {}}
        instructions["instructions"]["dataset_mapping"]["lidar"] = dict(zip(df['name'], df['id']))
        instructions["instructions"]["dataset_mapping"]["lidar"]["Unknown"] = 0
    with open(instructions_file, 'w') as f:
        json.dump(instructions, f, indent=2)


def get_geometry_by_file(boundary_file: Union[str, pathlib.Path], buffer: Union[int, float] = 0) -> shapely.geometry:
    """
    Read boundary geometry boundary_file, and return the buffered geometry.
    """
    gdf = gpd.read_file(boundary_file, driver='GeoJSON')
    if '2193' not in str(gdf.crs):
        gdf = gdf.to_crs(epsg=2193)
    gdf['buffered'] = gdf['geometry'].buffer(buffer, join_style='mitre')
    return (gdf['geometry'].buffer(buffer, join_style='mitre').values[0]
            if buffer != 0 else gdf['geometry'].values[0])


def retrieve_dataset(engine,
                     boundary_file: Union[str, pathlib.Path],
                     instructions: dict,
                     sort_by: str = 'survey_end_date',
                     buffer: Union[int, float] = 0) -> tuple:
    """
    Read boundary geometry boundary_file,
    Query dataset to get dataset name which covers the geometry based on the boundary geometry,
    Sort the dataset name by 'sort_by', and return a dictionary of dataset name and crs.
    To safeguard the data/tile integrity, the geometry is buffered by 'buffer' distance, no buffer by default.
    :param engine: sqlalchemy engine
    :param boundary_file: boundary file path, geojson format. see demo examples in 'configs' directory.
    :param instructions: instructions dictionary. see base instructions file 'instructions.json' in 'configs' directory.
    :param sort_by: sort dataset name by this column, default is 'survey_end_date'.
    :param buffer: buffer for the boundary geometry, default is 0.
    """
    resolution = (instructions["instructions"]["output"]["grid_params"]["resolution"]
                  if 'resolution' in instructions["instructions"]["output"]["grid_params"]
                  else None)
    assert isinstance(resolution, int), f'Resolution is not defined in instructions or data type is not Integer.'
    # resolution * buffer_factor is the margin for aligning the grid, can be changed
    geometry = get_geometry_by_file(boundary_file, buffer=buffer)
    query = f"""SELECT name, {sort_by}, tile_path, geometry FROM dataset
                WHERE ST_Intersects(geometry, ST_SetSRID('{geometry}'::geometry, 2193)) ;"""
    gdf = gpd.read_postgis(query, engine, geom_col='geometry')
    gdf = gdf.sort_values(sort_by, ascending=False)  # latest/largest first
    dataset_name_list = gdf['name'].to_list()
    tile_path_list = gdf['tile_path'].to_list()
    dataset_list = [(n, {"crs": {"horizontal": 2193, "vertical": 7839}}) for n in dataset_name_list]
    return OrderedDict(dataset_list), geometry, tile_path_list


def retrieve_lidar(engine,
                   boundary_file: Union[str, pathlib.Path],
                   instructions: dict,
                   sort_by: str = 'survey_end_date',
                   buffer: Union[int, float] = 0) -> dict:
    """
    Read catchment geometry from boundary_file,
    query dataset to get dataset name which intersect with the input geometry,
    sort the dataset name by 'sort_by',
    then retrieve the .laz file path, tile index file from tile table and lidar table,
    in the end return a dictionary of dataset name, crs, .laz file path and tile index file.
    """
    datasets_dict, geometry, tile_path_list = retrieve_dataset(
        engine, boundary_file, instructions, sort_by, buffer=buffer
    )
    for dataset_name in datasets_dict.keys():
        query = f"""SELECT uuid, geometry FROM tile
                    WHERE ST_Intersects(geometry, ST_SetSRID('{geometry}'::geometry, 2193))
                    AND dataset = '{dataset_name}' ;"""
        gdf = gpd.read_postgis(query, engine, geom_col='geometry')
        assert len(gdf) > 0, f"{dataset_name} does not have any tile in the ROI geometry."
        uuid = tuple(gdf['uuid'].to_list()) if len(gdf) > 1 else str(f"({gdf['uuid']})")
        query = f"SELECT file_path FROM lidar WHERE uuid IN {uuid} ;"
        df = pd.read_sql(query, engine)
        assert len(df) > 0, f"{dataset_name} does not have any .laz file in the ROI geometry."
        datasets_dict[dataset_name]["file_paths"] = [
            pathlib.PurePosixPath(p) for p in sorted(df['file_path'].to_list())
        ]
        datasets_dict[dataset_name]["tile_index_file"] = (
            [pathlib.PurePosixPath(p) for p in tile_path_list if dataset_name in p][0] if not None else None
        )
        assert datasets_dict[dataset_name]["tile_index_file"] is not None, (
            f"{dataset_name} tile index file does not exist."
        )
        logging.debug(f'Dataset {dataset_name} has '
                      f'{len(datasets_dict[dataset_name]["file_paths"])} lidar files in '
                      f'ROI with buffer distance {buffer} mitre.')
    return datasets_dict


def retrieve_catchment(engine, boundary_file: Union[str, pathlib.Path], buffer: Union[int, float] = 0) -> list:
    """
    Read boundary geometry boundary_file,
    Query dataset to get catch_id which covers the geometry based on the boundary geometry,
    To safeguard the data/tile integrity, the geometry is buffered by resolution * buffer_factor, no buffer by default.

    :param engine: sqlalchemy engine
    :param boundary_file: boundary file path, geojson format. see demo example in 'configs' directory.
    :param buffer: buffer factor for the boundary geometry, default is 0.
    """
    geometry = get_geometry_by_file(boundary_file, buffer=buffer)
    query = f"""SELECT catch_id, geometry FROM catchment
                WHERE ST_Intersects(geometry, ST_SetSRID('{geometry}'::geometry, 2193)) ;"""
    gdf = gpd.read_postgis(query, engine, geom_col='geometry')
    catch_list = sorted(gdf['catch_id'].to_list())
    logger.info(f"Retrieved {len(catch_list)} catchments from catchment table:\n{catch_list}")
    return catch_list


def retrieve_dem(engine, boundary_file: Union[str, pathlib.Path], buffer: Union[int, float] = 0) -> pd.DataFrame:
    """
    Read boundary geometry boundary_file,
    Query dataset to get file path which covers the geometry based on the boundary geometry,
    To safeguard the data/tile integrity, geometry is buffered by 'buffer' distance, no buffer by default.

    :param engine: sqlalchemy engine
    :param boundary_file: boundary file path, geojson format. see demo example in 'configs' directory.
    :param buffer: buffer factor for the boundary geometry, default is 0.
    """
    catch_list = retrieve_catchment(engine, boundary_file, buffer)
    df = tables.get_data_by_id(engine, tables.DEM, catch_list, geom_col='')
    return df


def remove_holes(polygon: Union[Polygon, MultiPolygon],
                 keep_threshold: Union[int, float] = 10_000 * 10_000) -> Union[Polygon, MultiPolygon]:
    """
    Convert multipolygon to polygon.
    Keep holes that area are greater than area, 100 km2 by default.

    :param polygon: shapely Polygon or MultiPolygon
    :param keep_threshold: area threshold for keeping the holes, 100 km2 by default
    """
    if isinstance(polygon, Polygon):
        multipolygon = MultiPolygon([polygon])
    else:
        multipolygon = polygon

    list_parts = []
    for geom in multipolygon.geoms:
        list_interiors = []

        for interior in geom.interiors:
            p = Polygon(interior)
            if p.area > keep_threshold:
                list_interiors.append(interior)

        temp_polygon = Polygon(geom.exterior.coords, holes=list_interiors).buffer(0, join_style='mitre')
        # check validity
        # temp_polygon = make_valid(temp_polygon)
        list_parts.append(temp_polygon)

    return unary_union(list_parts)
    # return shapely.MultiPolygon(list_parts)


def filter_geometry(geometry: Union[shapely.Geometry, Polygon, MultiPolygon, GeometryCollection, gpd.GeoSeries],
                    resolution: Union[int, float] = CATCHMENT_RESOLUTION,
                    polygon_threshold: Union[int, float] = 100 * 100,
                    hole_threshold: Union[int, float] = 1_000 * 1_000) -> Union[Polygon, MultiPolygon]:
    """
    filter geometry, remove gaps, holes, tiny polygons and thin rectangle that no needed.

    :param geometry: input geometry
    :param resolution: resolution of the in put geometry in meters
    :param polygon_threshold: lower area threshold to filter out small polygons
    :param hole_threshold: lower area threshold to filter out small holes
    :return: filtered geometry
    """
    # combine geometry if possible
    if isinstance(geometry, gpd.GeoSeries):
        geometry = geometry.unary_union  # geopandas.GeoSeries.unary_union
    elif isinstance(geometry, (MultiPolygon, Polygon, GeometryCollection)):
        geometry = unary_union(geometry)  # shapely.unary_union
    else:
        raise ValueError('geometry is not a valid type', type(geometry))
    # remove polygons the under threshold
    if isinstance(geometry, Polygon):
        assert geometry.area >= polygon_threshold, 'Input geometry is smaller than threshold.'
    else:
        geometry = MultiPolygon([p for p in geometry.geoms if p.area > polygon_threshold])
    # remove spikes
    geometry = (geometry.buffer(-EPS, join_style='mitre').
                buffer(EPS*2, join_style='mitre').
                buffer(-EPS, join_style='mitre'))
    # clean geometry boundary
    eps = resolution / 2 - EPS
    geometry = (geometry.buffer(eps, join_style='mitre').
                buffer(-eps * 2, join_style='mitre').
                buffer(eps, join_style='mitre'))
    # remove holes
    geometry = remove_holes(geometry, keep_threshold=hole_threshold)

    return geometry


# @timeit
def fishnet(geometry: shapely.geometry, threshold: Union[int, float]) -> list:
    """
    create fishnet grid based on the geometry and threshold
    """
    logging.info(f'Create fishnet grid with threshold {threshold}...')
    bounds = geometry.bounds
    xmin = int(bounds[0] // threshold)
    xmax = int(bounds[2] // threshold)
    ymin = int(bounds[1] // threshold)
    ymax = int(bounds[3] // threshold)
    # ncols = int(xmax - xmin + 1)
    # nrows = int(ymax - ymin + 1)
    result = []
    for i in range(xmin, xmax+1):
        for j in range(ymin, ymax+1):
            b = box(i*threshold, j*threshold, (i+1)*threshold, (j+1)*threshold)
            g = geometry.intersection(b)
            if g.is_empty:
                continue
            result.append(g)
    return result


# @timeit
def katana(geometry: shapely.geometry, threshold: Union[int, float], count: int = 0) -> list:
    """Split a Polygon into two parts across its shortest dimension if area is greater than threshold."""
    bounds = geometry.bounds
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    if geometry.area <= threshold or count == 250:
        # either the polygon is smaller than the threshold, or the maximum
        # number of recursions has been reached
        return [geometry]
    if height >= width:
        # split left to right
        a = box(bounds[0], bounds[1], bounds[2], bounds[1]+height/2)
        b = box(bounds[0], bounds[1]+height/2, bounds[2], bounds[3])
    else:
        # split top to bottom
        a = box(bounds[0], bounds[1], bounds[0]+width/2, bounds[3])
        b = box(bounds[0]+width/2, bounds[1], bounds[2], bounds[3])
    result = []
    for d in (a, b,):
        c = geometry.intersection(d)
        if not isinstance(c, GeometryCollection):
            c = [c]
        for e in c:
            if isinstance(e, (Polygon, MultiPolygon)):
                result.extend(katana(e, threshold, count+1))
    if count > 0:
        return result
    # convert multipart into single part
    final_result = []
    for g in result:
        if isinstance(g, MultiPolygon):
            final_result.extend(g.geoms)
        else:
            final_result.append(g)
    return final_result


# def gen_lidar_extent(engine, resolution: int = 8, area: int = 1024) -> gpd.GeoDataFrame:
def gen_table_extent(engine, table: Union[str, Type[Ttable]]) -> gpd.GeoDataFrame:
    """
    Generate catchment extent from catchment table
    """
    if not isinstance(table, str):
        table = table.__tablename__
    gdf = gpd.read_postgis(f"SELECT catch_id, geometry FROM {table}", engine, crs=2193, geom_col='geometry')
    geom = gdf['geometry'].unary_union
    return gpd.GeoDataFrame(index=[0], crs=gdf.crs, geometry=[geom])


def save_gpkg(gdf: gpd.GeoDataFrame, table: Union[Type[Ttable], str]):
    """
    Save source catchments to GPKG
    """
    gpkg_path = pathlib.Path(get_env_variable('DATA_DIR')) / pathlib.Path('GPKG')
    if isinstance(table, str):
        file_name = f'{table}.gpkg'
    else:
        file_name = f'{table.__tablename__}.gpkg'
    pathlib.Path(gpkg_path).mkdir(parents=True, exist_ok=True)
    gdf.set_crs(epsg=2193, inplace=True)
    gdf.to_file(str(gpkg_path / pathlib.Path(file_name)), driver='GPKG')
    logging.info(
        f"Save source catchments to {gpkg_path / pathlib.Path(file_name)}."
    )


def make_valid(geometry: shapely.geometry) -> shapely.geometry:
    """
    Returns a valid representation of the object.
    """
    if geometry.is_valid:
        return geometry
    return shapely.make_valid(geometry)


def get_min_width(geometry: shapely.geometry) -> float:
    """
    Get minimum width of the geometry
    """
    bounds = geometry.bounds
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    return min(width, height)


def delete_dir(directory: Union[str, pathlib.Path]) -> None:
    """
    Delete directory
    """
    if isinstance(directory, str):
        directory = pathlib.Path(directory)
    if directory.exists():
        shutil.rmtree(directory)
        logger.info(f'Delete directory {directory}.')


def get_netcdf_in_polygon(engine,
                          boundary_file: Union[str, pathlib.Path],
                          save_dir: Union[str, pathlib.Path],
                          save_name: str,
                          with_raw: bool = False,
                          buffer: Union[int, float] = 0) -> None:
    """
    Get netcdf files by boundary_file
    """
    # df_dem = retrieve_dem(engine, boundary_file, buffer)
    # hydro_dem_path = df_dem['hydro_dem_path'].tolist()
    hydro_dem_path = [r'C:\Users\xca30\GRI\datastorage\hydro_dem\13042395\13042395.nc',
                      r'C:\Users\xca30\GRI\datastorage\hydro_dem\13070004\13070004.nc']

    list_xds = []
    for file in hydro_dem_path:
        if pathlib.Path(file).exists():
            list_xds.append(xr.open_dataset(file))
        else:
            logger.warning(f'Expected Hydro DEM File {file} does not exist.')
    print(list_xds)
    xds = xr.combine_by_coords(list_xds, combine_attrs='drop',  compat='no_conflicts')
    # xds = xr.combine_by_coords(list_xds)
    xds = xds.rio.write_crs(2193)
    print(xds)
    geometry = get_geometry_by_file(boundary_file, buffer=buffer)
    xds_clipped = xds.rio.clip([geometry])
    print(xds_clipped)

    save_file = pathlib.Path(save_dir) / pathlib.Path(save_name)

    xds_clipped.to_netcdf(save_file)
    logger.info(f'Save clipped Hydro DEM to {save_file}.')

    # if with_raw:
    #     raw_dem_path = df_dem['raw_dem_path'].tolist()
    #     for file in raw_dem_path:
    #         if not pathlib.Path(file).exists():
    #             logger.warning(f'Expected Raw DEM File {file} does not exist.')
    #     xds = xr.open_mfdataset(raw_dem_path)
    #     xds_clipped = xds.rio.clip(geometry)
    #     save_file = pathlib.Path(save_dir) / pathlib.Path('raw_' + save_name)

    #     xds_clipped.to_netcdf(save_file)
    #     logger.info(f'Save clipped Raw DEM to {save_file}.')