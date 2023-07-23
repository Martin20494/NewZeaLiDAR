# -*- coding: utf-8 -*-
"""
This module is used to get LiDAR datasets information from opentopography.org and save to local database.
It leverages scrapy to crawl the website and download dataset metadata and extent files to local storage.
"""
import logging
import os
import pathlib
import re
from datetime import datetime

import geopandas as gpd
import pandas as pd
import scrapy
import scrapy.item
from fiona.drvsupport import supported_drivers
from scrapy.crawler import CrawlerProcess
from scrapy.pipelines.files import FilesPipeline
from scrapy.spiders import CrawlSpider

from src import utils
from src.tables import DATASET, create_table, delete_table, get_max_value

logger = logging.getLogger(__name__)

supported_drivers['LIBKML'] = 'rw'


def get_extent_geometry(extent_file: str) -> gpd.GeoSeries.values:
    """Get extent geometry from kml file."""
    if os.path.exists(extent_file):
        gdf = gpd.read_file(extent_file)
        gdf = gdf.to_crs(2193)
    else:
        raise ValueError(f'extent file {extent_file} is not exist.')
    return gdf['geometry'].values


def search_string(pattern: str, string: str) -> str:
    """Search string by pattern using regex."""
    match = re.search(pattern, string)
    if not match:
        raise ValueError(f'No target pattern: "{pattern}" found in string: "{string}".')
    return match.group(1)


# define item class to scrape
class DatasetItem(scrapy.Item):
    """A class to define item class to scrape."""
    dataset_url = scrapy.Field()
    file_urls = scrapy.Field()  # for download automatically by scrapy.pipelines.files.FilesPipeline
    name = scrapy.Field()
    # ot_id = scrapy.Field()
    describe = scrapy.Field()
    # collector = scrapy.Field()
    survey_start_date = scrapy.Field()
    survey_end_date = scrapy.Field()
    point_cloud_density = scrapy.Field()
    meta_path = scrapy.Field()
    extent_path = scrapy.Field()
    tile_path = scrapy.Field()
    datum = scrapy.Field()


class ExtraFilesPipeline(FilesPipeline):
    """
    The class to define the process pipeline after crawling items.
    In detail, it renames download files and saves items to database.
    Check https://docs.scrapy.org/en/latest/topics/item-pipeline.html for more details.
    """

    def file_path(self, request, response=None, info=None, *, item=None):
        """Rename downloaded files."""
        end_str = request.url.split('=')[-1]
        if end_str == 'xml':
            file_path = str(pathlib.PurePosixPath(item['meta_path']))
        elif end_str == 'true':  # kml url is ended with "download=true"
            file_path = str(pathlib.PurePosixPath(item['extent_path']))
        else:
            raise ValueError(f'input url {request.url} is not correct.')
        return file_path

    def item_completed(self, results, item, info):
        """Save crawled data to database."""
        engine = utils.get_database()
        create_table(engine, DATASET)
        timestamp = pd.Timestamp.now().strftime('%Y-%m-%d %X')
        data = {'id': '-1',
                'name': item['name'],
                # 'ot_id': item['ot_id'],
                'describe': item['describe'],
                # 'collector': item['collector'],
                'survey_start_date': item['survey_start_date'],
                'survey_end_date': item['survey_end_date'],
                'point_cloud_density': item['point_cloud_density'],
                'original_datum': item['datum'],
                'meta_path': item['meta_path'],
                'meta_source': item['file_urls'][0],
                'extent_path': item['extent_path'],
                # 'extent_source': item['file_urls'][1],
                'tile_path': item['tile_path'],
                'geometry': get_extent_geometry(item['extent_path']),
                'created_at': timestamp,
                'updated_at': timestamp}
        gdf_to_db = gpd.GeoDataFrame(data)
        query = f"""SELECT * FROM {DATASET.__tablename__} WHERE name = '{item["name"]}' ;"""
        gdf_from_db = gpd.read_postgis(query, engine, geom_col='geometry')
        if gdf_from_db.empty:
            _id = get_max_value(engine, 'dataset')
            gdf_to_db['id'] = _id + 1 if _id else 1
        else:
            delete_table(engine, DATASET, 'name', item['name'])
            # keep the 'created_at', 'id' and update the rest columns.
            gdf_to_db['id'] = gdf_from_db['id'].copy()
            gdf_to_db['created_at'] = gdf_from_db['created_at'].copy()
        gdf_to_db = gdf_to_db[['id',
                               'name',
                               # 'ot_id',
                               'describe',
                               'survey_start_date',
                               'survey_end_date',
                               'point_cloud_density',
                               'original_datum',
                               # 'collector',
                               'meta_path',
                               'meta_source',
                               'extent_path',
                               # 'extent_source',
                               'tile_path',
                               'geometry',
                               'created_at',
                               'updated_at']]
        gdf_to_db.to_postgis('dataset', engine, index=False, if_exists="append")
        # check_table_duplication(engine, DATASET, 'name')
        engine.dispose()
        return item


class DatasetSpider(CrawlSpider):
    """
    The class to define spider class to scrape defined items.
    Check https://docs.scrapy.org/en/latest/topics/spiders.html for more details.
    """
    name = "dataset"

    # custom_settings = {
    #     'LOG_LEVEL': 'INFO',
    # }  # not working

    allowed_domains = ['portal.opentopography.org']

    def __init__(self, data_dir, *a, **kw):
        super(DatasetSpider, self).__init__(*a, **kw)
        self.data_dir = data_dir
        self.start_urls = ('https://portal.opentopography.org/',)

    def start_requests(self):
        urls = [
            'https://portal.opentopography.org/ajaxOTDatasets?search=new%20zealand'
        ]  # only one url now, but keep it in a list for further reuse.
        for url in urls:
            yield scrapy.Request(url=url, callback=self.parse_table)

    def parse_table(self, response):
        for url in response.xpath(
                '//a[text()="Full Dataset Metadata"]/@href'
        ).extract():
            url = response.urljoin(url)
            yield scrapy.Request(url, callback=self.parse_metadata)

    def parse_metadata(self, response):
        item = DatasetItem()
        item['name'] = response.xpath(
            '//strong[text()="Short Name"]/following-sibling::text()[1]'
        ).extract()[0].split(':')[-1].strip()
        # item['ot_id'] = response.xpath(
        #     '//strong[text()="OT Collection ID"]/following-sibling::text()[1]'
        # ).extract()[0].split(':')[-1].strip()
        item['describe'] = response.xpath(
            '//strong[text()="OT Collection Name"]/following-sibling::text()[1]'
        ).extract()[0].split(':')[-1].strip()
        survey_date = response.xpath(
            '//strong[text()="Survey Date"]/following-sibling::text()'
        ).extract()[0].strip()
        survey_date = survey_date.split('-')
        if len(survey_date) == 1:  # no start date
            date = search_string(r'(\d{2}/\d{2}/\d{4})', survey_date[0])
            item['survey_start_date'] = item['survey_end_date'] = (
                datetime.strptime(date, '%m/%d/%Y').strftime('%Y-%m-%d')
            )
        elif len(survey_date) == 2:
            date = search_string(r'(\d{2}/\d{2}/\d{4})', survey_date[0])
            item['survey_start_date'] = datetime.strptime(date, '%m/%d/%Y').strftime('%Y-%m-%d')
            date = search_string(r'(\d{2}/\d{2}/\d{4})', survey_date[1])
            item['survey_end_date'] = datetime.strptime(date, '%m/%d/%Y').strftime('%Y-%m-%d')
        # item['collector'] = response.xpath(
        #     '//text()[contains(.,"Collector")]/following-sibling::ul[1]//text()'
        # ).extract()
        item['point_cloud_density'] = response.xpath(
            '//strong[text()="Point Density"]/following-sibling::text()[1]'
        ).extract()[0].split()[1].strip()
        datum = response.xpath(
            '//text()[contains(., "Vertical:")]'
        ).extract()[0].strip()
        item['datum'] = search_string(r'Vertical: (\w+\s*\d+)', datum)
        item['dataset_url'] = response.url
        file_urls = response.xpath(
            '//a[text()="ISO 19115 (Data)" or starts-with(@href, "/getKml")]/@href'
        ).extract()
        file_urls = [response.urljoin(u) for u in file_urls]
        item['file_urls'] = file_urls
        data_path = pathlib.Path(self.data_dir) / pathlib.Path(item['name'])
        item['meta_path'] = str(pathlib.PurePosixPath(data_path / pathlib.Path(item['name'] + '_Meta.xml')))
        item['extent_path'] = str(pathlib.PurePosixPath(data_path) / pathlib.Path(item['name'] + '_Extent.kml'))
        item['tile_path'] = str(pathlib.PurePosixPath(data_path) / pathlib.Path(item['name'] + '_TileIndex.zip'))
        yield item


def run() -> None:
    """
    Crawl the data from the website, save in the database,
    and save the metadata and extent files in the local directory.
    """
    logger.info('Start crawling datasets from OpenTopography.')
    process = CrawlerProcess({
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 6.1; WOW64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/34.0.1847.131 Safari/537.36',
        'DOWNLOAD_DELAY': 1.5,  # to avoid request too frequently and get incomplete response.
        'ITEM_PIPELINES': {'datasets.ExtraFilesPipeline': 1},
        'FILES_STORE': './',
        'LOG_LEVEL': 'INFO',  # this setting does not working. custom_settings does not work neither. TODO: debug
    })
    process.crawl(
        DatasetSpider,
        # data_dir for DatasetSpider init
        str(pathlib.Path(utils.get_env_variable('DATA_DIR')) / pathlib.Path(utils.get_env_variable('LIDAR_DIR')))
    )
    process.start()
    logger.info('Finish crawling datasets from OpenTopography.')


if __name__ == '__main__':
    run()