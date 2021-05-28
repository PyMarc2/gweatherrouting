# -*- coding: utf-8 -*-
# Copyright (C) 2017-2021 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import gi
import time
import cairo
import numpy as np
from os import listdir
from os.path import isfile, join
from osgeo import ogr, osr, gdal
from threading import Thread, Lock

gi.require_version("Gtk", "3.0")
gi.require_version('OsmGpsMap', '1.0')

from gi.repository import Gtk, Gio, GObject, OsmGpsMap, Gdk
from .chartlayer import ChartLayer

# https://gdal.org/tutorials/raster_api_tut.html

def datasetBBox(dataset):
		old_cs = osr.SpatialReference()
		old_cs.ImportFromWkt(dataset.GetProjectionRef())

		wgs84_wkt = """
		GEOGCS["WGS 84",
			DATUM["WGS_1984",
				SPHEROID["WGS 84",6378137,298.257223563,
					AUTHORITY["EPSG","7030"]],
				AUTHORITY["EPSG","6326"]],
			PRIMEM["Greenwich",0,
				AUTHORITY["EPSG","8901"]],
			UNIT["degree",0.01745329251994328,
				AUTHORITY["EPSG","9122"]],
			AUTHORITY["EPSG","4326"]]"""
		new_cs = osr.SpatialReference()
		new_cs .ImportFromWkt(wgs84_wkt)

		transform = osr.CoordinateTransformation(old_cs,new_cs) 

		width = dataset.RasterXSize
		height = dataset.RasterYSize
		gt = dataset.GetGeoTransform()
		minx = gt[0]
		miny = gt[3]

		origin = transform.TransformPoint(minx,miny) 

		minx = gt[0] + width*gt[1] + height*gt[2] 
		miny = gt[3] + width*gt[4] + height*gt[5] 

		originbr = transform.TransformPoint(minx,miny) 

		return [origin, originbr]
	
class GDALSingleRasterChart:
	def getFileInfo(path):
		dataset = gdal.Open(path, gdal.GA_ReadOnly)

		if not dataset:
			raise ("Unable to open raster chart %s" % path)

		bbox = datasetBBox(dataset)

		return (path, bbox, dataset.RasterXSize, dataset.RasterYSize)


	def __init__(self, path):
		dataset = gdal.Open(path, gdal.GA_ReadOnly)

		if not dataset:
			raise ("Unable to open raster chart %s" % path)

		# print("Driver: {}/{}".format(dataset.GetDriver().ShortName,
		# 					dataset.GetDriver().LongName))
		# print("Size is {} x {} x {}".format(dataset.RasterXSize,
		# 									dataset.RasterYSize,
		# 									dataset.RasterCount))
		geotransform = dataset.GetGeoTransform()

		self.geotransform = geotransform
		self.dataset = dataset
		self.bbox = datasetBBox(dataset)
		self.surface = self.bandToSurface(1)

	def bandToSurface(self, i):
		band = self.dataset.GetRasterBand(i)

		min = band.GetMinimum()
		max = band.GetMaximum()
		if not min or not max:
			(min,max) = band.ComputeRasterMinMax(True)

		colors = {}
		ct = band.GetRasterColorTable()
		
		for x in range(0, int(max) + 1):
			try: 
				c = ct.GetColorEntry(x)
			except:
				c = (0, 0, 0, 0)
				
			colors[x] = c[0]*0xFFFFFF + c[1]*0xFFFF + c[2] *0xFF
			# colors[x] = int(hex(c[0])[2:][::-1], 16)*0xFFFF + int(hex(c[1])[2:][::-1], 16)*0xFF + int(hex(c[2])[2:][::-1], 16) *0x1

		data = np.vectorize(lambda x: colors[x], otypes=[np.int32])(band.ReadAsArray())
		return cairo.ImageSurface.create_for_data(data, cairo.Format.RGB24, band.XSize, band.YSize, cairo.Format.RGB24.stride_for_width(band.XSize))


	def do_draw(self, gpsmap, cr):
		p1, p2 = gpsmap.get_bbox()
		p1lat, p1lon = p1.get_degrees()
		p2lat, p2lon = p2.get_degrees()

		cr.save()
		xx, yy = gpsmap.convert_geographic_to_screen(
			OsmGpsMap.MapPoint.new_degrees(self.bbox[0][0], self.bbox[0][1])
		)
		xx2, yy2 = gpsmap.convert_geographic_to_screen(
			OsmGpsMap.MapPoint.new_degrees(self.bbox[1][0], self.bbox[1][1])
		)
		scalingy = (yy2 - yy) / self.dataset.RasterYSize
		scalingx = (xx2 - xx) / self.dataset.RasterXSize
		cr.translate(xx, yy)
		cr.scale(scalingx, scalingy)
		cr.set_source_surface(self.surface, 0,0)
		cr.paint()
		cr.restore()




class GDALRasterChart(ChartLayer):
	def __init__(self, path, metadata = None):
		super().__init__(path, 'raster', metadata)
		self.cached = {}
		self.loadLock = Lock()

		if metadata:
			self.onMetadataUpdate()


	def onMetadataUpdate(self):
		pass

	def onRegister(self, onTickHandler = None):
		self.metadata = []

		files = [f for f in listdir(self.path) if isfile(join(self.path, f))]
		i = 0

		for x in files:
			finfo = GDALSingleRasterChart.getFileInfo(self.path + x)
			self.metadata.append(finfo)

			i += 1
			if onTickHandler:
				onTickHandler(i/len(files))

		self.onMetadataUpdate(metadata)

		if onTickHandler:
			onTickHandler(1.0)

		return True

	lastRect = None
	lastRasters = None


	def loadRaster(self, gpsmap, path):
		with self.loadLock:
			print ('Loading', path)
			r = GDALSingleRasterChart(path)
			self.cached[path] = r
			print ('Done loading', path)
			Gdk.threads_enter()
			gpsmap.queue_draw()
			Gdk.threads_leave()
		

	def do_draw(self, gpsmap, cr):
		p1, p2 = gpsmap.get_bbox()
		p1lat, p1lon = p1.get_degrees()
		p2lat, p2lon = p2.get_degrees()
		scale = gpsmap.get_scale()

		# Check if bounds hasn't changed
		# if self.lastRasters and self.lastRect == [p1lat, p1lon, p2lat, p2lon]:
		# 	for x in self.lastRasters:
		# 		x.do_draw(gpsmap, cr)
		# 	return

		# Estimate which raster is necessary given bound and zoom
		minBLat = min(p1lat, p2lat)
		maxBLat = max(p1lat, p2lat)
		minBLon = min(p1lon, p2lon)
		maxBLon = max(p1lon, p2lon)

		toload = []
		for x in self.metadata:
			bb, path, sizeX, sizeY = x[1], x[0], x[2], x[3]

			minRLat = min(bb[0][0], bb[1][0])
			maxRLat = max(bb[0][0], bb[1][0])
			minRLon = min(bb[0][1], bb[1][1])
			maxRLon = max(bb[0][1], bb[1][1])

			inside = minRLat > minBLat and maxRLat < maxBLat and minRLon > minBLon and maxRLon < maxBLon
			a = minRLat < minBLat and maxRLat > maxBLat and minRLon > minBLon and minRLon < maxBLon
			b = minRLat < minBLat and maxRLat > maxBLat and minRLon < minBLon and maxRLon > minBLon
			c = minRLat < minBLat and maxRLat > minBLat and minRLon < minBLon and maxRLon > maxBLon
			d = minRLat < maxBLat and maxRLat > maxBLat and minRLon < minBLon and maxRLon > maxBLon

			area = (((maxRLat + 90) - (minRLat + 90))) * (((maxRLon + 180) - (minRLon + 180)))

			if a or b or c or d:
				toload.append([path, bb, area, inside])

		toload.sort(key=lambda x: x[2])
		toload.reverse()

		# print (len(toload), len(self.metadata))


		# Check which rasters are already loaded
		rasters = []

		for x in toload:
			if x[0] in self.cached:
				if self.cached[x[0]] != 'loading':
					rasters.append(self.cached[x[0]])
				continue
				
			# print ('Loading', x)
			# r = GDALSingleRasterChart(x[0])
			# self.cached[x[0]] = r
			# rasters.append(r)
			self.cached[x[0]] = 'loading'

			t = Thread(target=self.loadRaster, args=(gpsmap, x[0],))
			t.daemon = True
			t.start()


		# Save and render
		self.lastRect = [p1lat, p1lon, p2lat, p2lon]
		self.lastRasters = rasters
		
		for x in rasters:
			x.do_draw(gpsmap, cr)

	def do_render(self, gpsmap):
		pass

	def do_busy(self):
		return False

	def do_button_press(self, gpsmap, gdkeventbutton):
		return False
