# -*- coding: utf-8 -*-
# usage: in prompt of conda environment containing pdal, set correct paths, run the script. e.g.:
#        > conda activate lidar
#        > python NewZeaLiDAR/scripts/datum_lvd_nzvd2016.py 3  # 3 is the index of path_list

import os
import sys
import pathlib
from functools import partial
import subprocess
import multiprocessing as mp
from multiprocessing.pool import ThreadPool


# print('Work dir: ', pathlib.Path.cwd())
path_list = [
    # not support space in path, need modify directory name from 'Processed Point Cloud' to 'Processed_Point_Cloud'
    r'./datastorage/lidar/NZ10_WHope',
    r'./datastorage/lidar/NZ10_CAlpine',
    r'./datastorage/lidar/NZ10_Wellington',
    r'./datastorage/lidar_waikato/LiDAR_2014_Hipaua_Thermal_Area/Moturiki1953/Processed_Point_Cloud',
    r'./datastorage/lidar_waikato/LiDAR_2012_2013_Coromandel/Auckland_1946/Processed_Point_Cloud',
    r'./datastorage/lidar_waikato/LiDAR_2010_2011/Northern_Waikato/Processed_Point_Cloud/Moturiki_1953',
    r'./datastorage/lidar_waikato/LiDAR_2010_2011/Raglan_Harbour/Processed_Point_Cloud/Moturiki_1953',
    r'./datastorage/lidar_waikato/LiDAR_2007_2008/Area_1/XYZI/ground',
    r'./datastorage/lidar_waikato/LiDAR_2007_2008/Area_1_Option_B/XYZI/ground',
    r'./datastorage/lidar_waikato/LiDAR_2007_2008/Area_2/XYZI/ground',
    r'./datastorage/lidar_waikato/LiDAR_2007_2008/Area_3/XYZI/ground',
    r'./datastorage/lidar_waikato/LiDAR_2007_2008/Area_4/XYZI/ground',
    r'./datastorage/lidar_waikato/LiDAR_2007_2008/Area_5/XYZI/ground',
    r'./datastorage/lidar_waikato/LiDAR_2007_2008/Area_6/XYZI/ground',
    r'./datastorage/lidar_waikato/LiDAR_2006_Lake_Taupo/Moturiki_1953/ground',
]
assert int(sys.argv[1]) < len(path_list), 'Input Index out of range!'
src_dir = pathlib.Path(path_list[int(sys.argv[1])])
print('Transforming datum from LVD to NZVD2016 in dir:\n', src_dir)

# for Lyttleton_1937: NZ10_WHope NZ10_CAlpine, Wellht_1953: NZ10_Wellington
if 'NZ10_WHope' in str(src_dir) or 'NZ10_CAlpine' in str(src_dir) or 'NZ10_Wellington' in str(src_dir):
    # change .laz file suffix to .las. PDAL treat .laz as .las, so it is alright to change suffix
    laz_files = [f for f in src_dir.glob('*.laz')]
    for f in laz_files:
        f.rename(src_dir / pathlib.Path(f.stem + '.las'))

# gtx file source: https://github.com/linz/proj-datumgrid-nz
# git clone https://github.com/linz/proj-datumgrid-nz.git
gtxfile_Moturiki_1953 = r'./proj-datumgrid-nz/files/moturiki_1953.gtx'
gtxfile_Auckland_1946 = r'./proj-datumgrid-nz/files/auckht1946-nzvd2016.gtx'
gtxfile_Lyttleton_1937 = r'./proj-datumgrid-nz/files/lyttht1937-nzvd2016.gtx'
gtxfile_Wellington_1953 = r'./proj-datumgrid-nz/files/wellht1953-nzvd2016.gtx'
if 'NZ10_WHope' in str(src_dir) or 'NZ10_CAlpine' in str(src_dir):
    gtxfile = gtxfile_Lyttleton_1937
elif 'NZ10_Wellington' in str(src_dir):
    gtxfile = gtxfile_Wellington_1953
elif 'Auckland_1946' in str(src_dir):
    gtxfile = gtxfile_Auckland_1946
else:
    gtxfile = gtxfile_Moturiki_1953

# pipeline files
pipeline_las = r'./NewZeaLiDAR/configs/pipeline_las.json'
pipeline_xyz = r'./NewZeaLiDAR/configs/pipeline_xyz.json'
assert os.path.exists(pipeline_las), 'pipeline_las.json not found!'
assert os.path.exists(pipeline_xyz), 'pipeline_xyz.json not found!'
horizontal_srs = 'EPSG:2193'

if 'NZ10_WHope' in str(src_dir) or 'NZ10_CAlpine' in str(src_dir) or 'NZ10_Wellington' in str(src_dir):
    dest_dir = src_dir / pathlib.Path('NZVD2016')
else:  # waikato_lidar
    dest_dir = src_dir.parent.parent / pathlib.Path('NZVD2016')
dest_dir.mkdir(parents=True, exist_ok=True)

pdal_cmd_list = []

for (path, _, files) in os.walk(src_dir):
    for file in files:
        src_file = str(pathlib.Path(path) / pathlib.Path(file))
        assert os.path.exists(src_file), f'{src_file} not found!'
        pdal_cmd = ''

        if file.lower().endswith('.las'):
            pipeline = pipeline_las
            if '.las' in file:
                file = file.replace('.las', '.laz')
            if '.LAS' in file:
                file = file.replace('.LAS', '.laz')
            dest_file = str(dest_dir / pathlib.Path(file))
            # print(f'Re-projecting {file} with {pipeline} and {gtxfile}...')
            pdal_cmd = 'pdal pipeline {} ' \
                       '--readers.las.filename={} ' \
                       '--writers.las.filename={} ' \
                       '--filters.reprojection.out_srs="+init={} +geoidgrids={}"'.format(
                           pipeline, src_file, dest_file, horizontal_srs, gtxfile)

        if file.lower().endswith('.xyz') or file.lower().endswith('.xyzi'):
            pipeline = pipeline_xyz
            if '.XYZI' in file:
                file = file.replace('.XYZI', '.laz')
            if '.xyz' in file:
                file = file.replace('.xyz', '.laz')
            if '.XYZ' in file:
                file = file.replace('.XYZ', '.laz')
            dest_file = str(dest_dir / pathlib.Path(file))
            # print(f'Re-projecting {file} with {pipeline} and {gtxfile}...')
            pdal_cmd = 'pdal pipeline {} ' \
                       '--readers.text.filename={} ' \
                       '--writers.las.filename={} ' \
                       '--filters.reprojection.out_srs="+init={} +geoidgrids={}"'.format(
                           pipeline, src_file, dest_file, horizontal_srs, gtxfile)

        if pdal_cmd != '':
            pdal_cmd_list.append(pdal_cmd)

print(f'Transferring datum for {len(pdal_cmd_list)} lidar files...')

with ThreadPool(mp.cpu_count()) as pool:
    pool.map(partial(subprocess.run, shell=True, check=True, text=True, capture_output=True), pdal_cmd_list)

if 'NZ10_WHope' in str(src_dir) or 'NZ10_CAlpine' in str(src_dir) or 'NZ10_Wellington' in str(src_dir):
    laz_files = [f for f in dest_dir.glob('*.laz')]
    las_files = [f for f in src_dir.glob('*.las')]
    if len(laz_files) != len(pdal_cmd_list):
        # if no file transferred successfully, recover the original file name
        if len(laz_files) == 0:
            for f in las_files:
                f.rename(src_dir / pathlib.Path(f.stem + '.laz'))
        raise Exception('Number of laz files not equal to number of pdal commands!')
    # move laz files to src_dir
    for f in laz_files:
        f.rename(src_dir / pathlib.Path(f.name))
    # delete las files
    for f in las_files:
        f.unlink()
    dest_dir.rmdir()

with open(src_dir / 'datum.log', 'w') as file:
    file.write('\n'.join(pdal_cmd_list))

print('Done!')
