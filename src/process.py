# -*- coding: utf-8 -*-
"""
This module is used to build process pipeline that generate hydrological conditioned DEMs from LiDAR for specific
catchment geometry.

Prerequisites:
* catchment list is ready: run catchments module to download catchments data and save to local database.
* dataset table is ready: run datasets module to download dataset metadata and extent files to local storage.
* lidar and tile files and tables is ready: run lidar module to download lidar data and save to local database.
"""
import gc
import time
import json
import logging
import os
import pathlib
import sys
from datetime import datetime, timedelta
from typing import Union, Type
import geopandas as gpd
import pandas as pd
from collections import OrderedDict

from src import utils
from src.tables import (Ttable, SDC, CATCHMENT, DEM, create_table,
                        get_data_by_id, get_split_catchment_by_id, get_id_under_area, check_table_duplication)

from geofabrics import processor

logger = logging.getLogger(__name__)

# Use Fork GeoFabrics
# sys.path.insert(0, str(pathlib.Path(r'../ForkGeoFabrics/src/geofabrics')))

# # for dask, set check time to 3 hours (or other appropriate value) to eliminate overwhelming warning like:
# #     distributed.core - INFO - Event loop was unresponsive in Worker for 3.90s.
# #     This is often caused by long-running GIL-holding functions or moving large chunks of data.
# #     This can cause timeouts and instability.
# os.environ["DASK_DISTRIBUTED__ADMIN__TICK__LIMIT"] = "3h"
# print("DASK_DISTRIBUTED__ADMIN__TICK__LIMIT = ", os.environ["DASK_DISTRIBUTED__ADMIN__TICK__LIMIT"])


def save_instructions(instructions: OrderedDict, instructions_path: str) -> None:
    """save instructions to json file."""

    def _recursive_str(d):
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, dict):
                    _recursive_str(v)
                else:
                    d[k] = str(v)
        else:
            d = str(d)
        return d

    with open(instructions_path, 'w') as f:
        json.dump(instructions, f, default=_recursive_str, indent=2)


def gen_instructions(engine: object,
                     instructions: OrderedDict,
                     index: int,
                     mode: str = 'api',
                     buffer: Union[int, float] = 0) -> OrderedDict:
    """Read basic instruction file and adds keys and uses geojson as catchment_boundary"""
    n_cpu = os.cpu_count()
    instructions["instructions"]["processing"]["number_of_cores"] = n_cpu - 1 if n_cpu > 8 else n_cpu
    data_dir = pathlib.PurePosixPath(utils.get_env_variable("DATA_DIR"))
    dem_dir = pathlib.PurePosixPath(utils.get_env_variable("DEM_DIR"))
    index_dir = pathlib.PurePosixPath(str(index))
    # subfolder_for_files = pathlib.PurePosixPath(dem_dir / index_dir)
    # subfolder_for_instructions = pathlib.PurePosixPath(f'../{str(subfolder_for_files)}')
    subfolder = pathlib.PurePosixPath(dem_dir / index_dir)

    if instructions["instructions"].get("data_paths") is None:
        instructions["instructions"]["data_paths"] = OrderedDict()
    instructions["instructions"]["data_paths"]["local_cache"] = str(data_dir)
    instructions["instructions"]["data_paths"]["subfolder"] = str(subfolder)
    instructions["instructions"]["data_paths"]["downloads"] = str(data_dir)
    instructions["instructions"]["data_paths"]["result_dem"] = f'{index}.nc'
    instructions["instructions"]["data_paths"]["raw_dem"] = f'{index}_raw_dem.nc'
    instructions["instructions"]["data_paths"]["raw_dem_extents"] = f'{index}_raw_extents.geojson'
    instructions["instructions"]["data_paths"]["catchment_boundary"] = f'{index}.geojson'
    if utils.get_env_variable("LAND_FILE") is not None:
        # cwd is in dem_dir: datastorage/HydroDEM/index
        instructions["instructions"]["data_paths"]["land"] = str(
            f"../../{str(pathlib.PurePosixPath(utils.get_env_variable('LAND_FILE')))}"
        )
    catchment_boundary_file = str(pathlib.PurePosixPath(data_dir / subfolder / pathlib.Path(f'{index}.geojson')))

    if instructions["instructions"].get("datasets") is None:
        instructions["instructions"]["datasets"] = OrderedDict({"lidar": {}})
    if mode == 'api':
        if instructions["instructions"]["data_paths"].get("land") is None:
            if instructions["instructions"]["datasets"].get("vector") is None:
                instructions["instructions"]["datasets"]["vector"] = OrderedDict({"linz": {}})
            instructions["instructions"]["datasets"]["vector"]["linz"]["key"] = utils.get_env_variable("LINZ_API_KEY")
            instructions["instructions"]["datasets"]["vector"]["linz"]["land"] = {"layers": [51153]}
        instructions["instructions"]["datasets"]["lidar"]["open_topography"] = (
            utils.retrieve_dataset(engine,
                                   catchment_boundary_file,
                                   instructions,
                                   'survey_end_date',
                                   buffer=buffer)[0])
        instructions["instructions"]["datasets"]["lidar"]["local"] = OrderedDict()
    if mode == 'local':
        instructions["instructions"]["datasets"]["lidar"]["local"] = (
            utils.retrieve_lidar(engine,
                                 catchment_boundary_file,
                                 instructions,
                                 'survey_end_date',
                                 buffer=buffer))
        instructions["instructions"]["datasets"]["lidar"]["open_topography"] = OrderedDict()

    # for debug
    instructions_path = str(pathlib.PurePosixPath(data_dir /
                                                  subfolder /
                                                  pathlib.Path('instructions.json')))
    save_instructions(instructions, instructions_path)
    return instructions


def gen_dem(instructions) -> None:
    """Use geofabrics to generate the hydrologically conditioned DEM."""
    runner = processor.RawLidarDemGenerator(instructions["instructions"], debug=False)
    runner.run()
    runner = processor.HydrologicDemGenerator(instructions["instructions"], debug=False)
    runner.run()


def single_process(engine: object,
                   instructions: OrderedDict,
                   index: int,
                   mode: str = 'api',
                   buffer: Union[int, float] = 0) -> Union[OrderedDict, None]:
    """the gen_dem process in a single row of geodataframe"""
    logger.info(f'*** Processing {index} in {mode} mode with geometry buffer {buffer} ...')
    single_instructions = gen_instructions(engine, instructions, index, mode=mode, buffer=buffer)
    if mode == 'api':
        if not single_instructions["instructions"]["datasets"]["lidar"]["open_topography"]:
            logger.info(f'The {index} catchment has no lidar data exist.')
            return None
    elif mode == 'local':
        if not single_instructions["instructions"]["datasets"]["lidar"]["local"]:
            logger.info(f'The {index} catchment has no lidar data exist.')
            return None
    else:
        raise ValueError(f'Invalid mode: {mode}')
    gen_dem(single_instructions)
    return single_instructions


def store_hydro_to_db(engine, table: Type[Ttable], instructions: OrderedDict) -> None:
    """save hydrological conditioned dem to database in hydro table."""
    assert len(instructions) > 0, 'instructions is empty dictionary.'
    index = os.path.basename(instructions["instructions"]["data_paths"]["subfolder"])
    dir_path = (pathlib.Path(instructions["instructions"]["data_paths"]["local_cache"]) /
                pathlib.Path(instructions["instructions"]["data_paths"]["subfolder"]))
    # {index}_raw_dem.nc
    raw_dem_path = str(dir_path /
                       pathlib.Path(instructions["instructions"]["data_paths"]["raw_dem"]))
    # {index}.nc
    result_dem_path = str(dir_path /
                          pathlib.Path(instructions["instructions"]["data_paths"]["result_dem"]))
    # {index}_raw_dem_extent.geojson
    raw_extent_path = str(dir_path /
                          pathlib.Path(instructions["instructions"]["data_paths"]["raw_dem_extents"]))
    raw_dem_path_exist = os.path.exists(raw_dem_path)
    result_dem_path_exist = os.path.exists(result_dem_path)
    raw_extent_path_exist = os.path.exists(raw_extent_path)
    assert raw_dem_path_exist, f'Warning: File {raw_dem_path} not exist.'
    assert result_dem_path_exist, f'Warning: File {result_dem_path} not exist.'
    assert raw_extent_path_exist, f'Warning: File {raw_extent_path} not exist.'
    timestamp = pd.Timestamp.now().strftime('%Y-%m-%d %X')
    create_table(engine, table)
    query = f"SELECT * FROM {table.__tablename__} WHERE catch_id = '{index}' ;"
    df_from_db = pd.read_sql(query, engine)
    if not df_from_db.empty:
        query = f"""UPDATE {table.__tablename__}
                    SET raw_dem_path = '{raw_dem_path}',
                        hydro_dem_path = '{result_dem_path}',
                        extent_path = '{raw_extent_path}',
                        updated_at = '{timestamp}'
                    WHERE catch_id = '{index}' ;"""
        engine.execute(query)
        logger.info(f'Updated {index} in {table.__tablename__} at {timestamp}.')
    else:
        query = f"""INSERT INTO {table.__tablename__} (
                    catch_id, 
                    raw_dem_path, 
                    hydro_dem_path, 
                    extent_path, 
                    created_at, 
                    updated_at
                    ) VALUES (
                    {index}, 
                    '{raw_dem_path}', 
                    '{result_dem_path}', 
                    '{raw_extent_path}', 
                    '{timestamp}', 
                    '{timestamp}'
                    ) ;"""
        engine.execute(query)
        logger.info(f'Add new {index} in {table.__tablename__} at {timestamp}.')
    # check_table_duplication(engine, table, 'catch_id')


def run(catch_id: Union[int, str, list] = None,
        area: Union[int, float] = None,
        mode: str = 'api',
        buffer: float = 10,
        gpkg: bool = False) -> None:
    """
    Main function for generate hydrological conditioned dem of catchments.
    :param catch_id: the id of target catchments, if id is negative, get all catchments in the catchment table.
    :param area: the upper limit area of target catchments.
    :param mode: 'api' or 'local', default is 'api'.
        If mode is 'api', the lidar data will be downloaded from open topography.
        If mode is 'local', the lidar data will be downloaded from local directory.
    :param buffer: the catchment boundary buffer for safeguard catchment boundary,
        default value is 10 meters.
    :param gpkg: if True, save the hydrological conditioned dem as geopackage.
    """
    engine = utils.get_database()
    data_dir = pathlib.Path(utils.get_env_variable("DATA_DIR"))
    dem_dir = pathlib.Path(utils.get_env_variable("DEM_DIR"))
    catch_path = data_dir / dem_dir
    instructions_file = pathlib.Path(utils.get_env_variable("INSTRUCTIONS_FILE"))
    with open(instructions_file, 'r') as f:
        instructions = json.loads(f.read(), object_pairs_hook=OrderedDict)

    # generate dataset mapping info
    utils.map_dataset_name_with_id(engine, 'dataset', instructions_file)

    # note the priority of selected catch_id > area limit > all catchments
    if catch_id is not None:
        catch_id = [catch_id] if not isinstance(catch_id, list) else catch_id
        logger.debug(f'check catch_id: {catch_id} is valid or not.')
        _gdf = pd.read_sql(f"SELECT catch_id FROM {SDC.__tablename__} ;", engine)
        sdc_id = sorted(_gdf['catch_id'].to_list())
        _gdf = pd.read_sql(f"SELECT catch_id FROM {CATCHMENT.__tablename__} ;", engine)
        catchment_id = sorted(_gdf['catch_id'].to_list())
        new_id = []
        for i in catch_id:
            if i in catchment_id:  # small catchment
                new_id.append(i)
            elif i in sdc_id and i not in catchment_id:  # large catchment, search subordinates
                _list = get_split_catchment_by_id(engine, i, sub=True)
                if len(_list) > 0:
                    new_id.extend(_list)
                    logger.debug(f'Catchment {i} split to {len(_list)} subordinates {_list}.')
                else:
                    logger.warning(f'Catchment {i} is not in catchment table, '
                                   f'please check if it is duplicated or overlap with other catchments.')
            else:
                logger.warning(f'Catchment {i} is not in catchment table, ignore it.')
        logger.debug(f'check catch_id: pass.')
        catch_id = new_id
    elif area is not None:
        catch_id = get_id_under_area(engine, SDC, area)
        logger.info(f'There are {len(catch_id)} Catchments that area is under {area} m2')
    elif catch_id < 0:
        _gdf = pd.read_sql(f"SELECT catch_id FROM {CATCHMENT.__tablename__} ;", engine)
        catch_id = sorted(_gdf['catch_id'].to_list())
        logger.info(f'******* FULL CATCHMENTS MODE *********\nThere are {len(catch_id)} Catchments in total.')
    else:
        raise ValueError('Please provide catch_id or area_limit.')

    # generate catchment boundary geodataframe
    gpkg_dir = pathlib.Path(utils.get_env_variable('DATA_DIR')) / pathlib.Path('GPKG')
    lidar_extent_file = gpkg_dir / pathlib.Path('lidar_extent.gpkg')
    pathlib.Path(gpkg_dir).mkdir(parents=True, exist_ok=True)
    if pathlib.Path(lidar_extent_file).exists():
        lidar_extent = gpd.read_file(lidar_extent_file)
    else:
        # generate lidar extent of all lidar datasets, to filter out catchments without lidar data
        lidar_extent = utils.gen_table_extent(engine, CATCHMENT)
        # save lidar extent to check on QGIS
        if gpkg:
            lidar_extent.to_file(str(gpkg_dir / pathlib.Path('lidar_extent.gpkg')), driver='GPKG')

    resolution = (instructions["instructions"]["output"]["grid_params"]["resolution"]
                  if 'resolution' in instructions["instructions"]["output"]["grid_params"]
                  else None)

    runtime = []
    failed = []
    for i in catch_id:
        catchment_boundary = get_data_by_id(engine, CATCHMENT, i)
        # check if catchment boundary of RoI within lidar extent
        if resolution and lidar_extent.buffer(buffer).intersects(catchment_boundary).any():
            # generate catchment boundary file for each catchment
            utils.gen_boundary_file(catch_path, catchment_boundary, i)
            # generate hydrological conditioned dem for each catchment
            start = datetime.now()
            try:
                single_instructions = single_process(engine, instructions, i, mode=mode, buffer=buffer)
            except Exception as e:
                logger.error(f'Error: {e}')
                failed.append(i)
                continue
            end = datetime.now()
            runtime.append(end - start)
            store_hydro_to_db(engine, DEM, single_instructions)
        elif resolution is None:
            logger.warning('Warning: resolution is None, please check the instructions file.')
        else:
            logger.info(f'Catchment {i} is not within lidar extent, ignor it.')

    # save lidar extent to check on QGIS
    if gpkg:
        df_extent = pd.read_sql(f"SELECT catch_id, extent_path FROM {DEM.__tablename__} ;", engine)
        df_extent['geometry'] = df_extent['extent_path'].apply(lambda x: gpd.read_file(x).geometry[0])
        gdf_extent = gpd.GeoDataFrame(df_extent[['catch_id', 'geometry']], crs='epsg:2193', geometry='geometry')
        geom_extent = gdf_extent['geometry'].unary_union
        gdf_extent = gpd.GeoDataFrame(index=[0], crs='epsg:2193', geometry=[geom_extent])
        gdf_extent.to_file(str(gpkg_dir / pathlib.Path(f'dem_extent_{time.strftime("%Y%m%d_%H%M%S")}.gpkg')),
                           driver='GPKG')
    if len(failed):
        logger.info(f'Failed {len(failed)} catchments: \n{failed}')
    logger.info(f"Total runtime: {sum(runtime, timedelta(0, 0))}\n"
                f"Runtime for each catch_id:{json.dumps(runtime, indent=2, default=str)}")
    engine.dispose()
    gc.collect()


if __name__ == '__main__':
    # catch_list = [1588, 1596]
    # catch_list = [1548, 1394]
    catch_list = 1394
    # run(catch_id=catch_list, mode='local')
    run(catch_id=catch_list, mode='api')
    # run(catch_id=1, mode='api', buffer=12)
    # run(area=1000000, mode='api', buffer=12)
    # run(area=1000000, mode='local', buffer=12)