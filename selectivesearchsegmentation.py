import sys
import random
import dataclasses

import numpy as np
import cv2
import cv2.ximgproc.segmentation

@dataclasses.dataclass(order = True)
class Region:
	rank : float
	id : int
	level : int
	merged_to : int
	bounding_box : cv2.Rect

	@property
	def bounding_box_tuple(self):
		return (self.bounding_box.x, self.bounding_box.y, self.bounding_box.width, self.bounding_box.height)

@dataclasses.dataclass(order = True)
class Neighbour:
	similarity : float
	from : int
	to : int
	removed : bool

class RegionSimilarity:
	def __init__(self, strategies):
		self.strategies = strategies
		self.weights = [1.0 / len(strategies)] * len(strategies)

	def __call__(self, r1, r2):
		return sum(self.weights[i] * s(r1, r2) for i, s in enumerate(self.strategies)) / sum(self.weights)

	def merge(self, r1, r2): 
		for s in self.strategies:
			s.merge(r1, r2);


def selectiveSearchFast(img_bgr, base_k = 150, inc_k = 150, sigma = 0.8, min_size = 100):
	segmentations = [ cv.ximgproc.segmentation.createGraphSegmentation(sigma, float(k), min_size) for k in range(base_k, 1 + base_k + inc_k * 2, inc_k) ]
	
	images = [ cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV), cv2.cvtColor(img_bgr, cv2.COLOR_BGR2Lab) ]
	
	all_regions = []

	for image in images:
		for gs in segmentations:
			img_regions = gs.processImage(image)
			nb_segs = int(img_regions.max()) + 1

			is_neighbour = np.zeros((nb_segs, nb_segs), dtype = np.bool)
			sizes = [0] * nb_segs

			previous_p = None
			
			points = [[] for i in range(nb_segs)]

			for i in range(img_regions.rows):
				for j in range(img_regions.cols): 
					p = img_regions[i]
					points[p[j]].append((j, i))
					sizes[p[j]] += 1

					if i > 0 && j > 0:
						is_neighbour[p[j - 1], p[j]] = is_neighbour[p[j], p[j - 1]] = True
						is_neighbour[previous_p[j], p[j]] = is_neighbour[p[j], previous_p[j]] = True
						is_neighbour[previous_p[j - 1], p[j]] = is_neighbour[p[j], previous_p[j - 1]] = True

				previous_p = p

			bounding_rects = [cv2.boundingRect(points[seg]) for seg in range(nb_segs)]
			features = HandcraftedRegionFeatures(image, img_regions, sizes)
			
			strategies = RegionSimilarity(RegionSimilarity(features.Color, features.Fill, features.Texture, features.Size), RegionSimilarity(features.Fill, features.Texture, features.Size))
			
			all_regions.extend( region for strategy in strategies for region in hierarchicalGrouping(strategy, is_neighbour, sizes, nb_segs, bounding_rects) )
			
	
	return list({region.bounding_box_tuple : True for region in sorted(all_regions, key = lambda r: r.bounding_box_tuple)}.keys())


def hierarchicalGrouping(s, is_neighbour, sizes, nb_segs, bounding_rects):
	sizes = sizes[:]
	regions, similarities = [], []

	for i in range(nb_segs):
		regions.append(Region(id = i, level = i, merged_to = -1, bounding_box = bounding_rects[i]))
		for j in range(i + 1, nb_segs):
		if is_neighbour[i, j]:
			similarities.append(Neighbour(from = i, to = j, similarity = s(i, j), removed = False))

	while len(similarities) > 0:
		similarities.sort()
		p = similarities.pop()

		region_from, region_to = regions[p.from], regions[p.to]
		regions.append(Region(id = min(region_from.id, region_to.id), level = max(region_from.level, region_to.level) + 1, merged_to = -1, bounding_box = region_from.bounding_box | region_to.bounding_box))
		regions[p.from].merged_to = regions[p.to].merged_to = len(regions) - 1

		s.merge(region_from.id, region_to.id);

		sizes[region_from.id] += sizes[region_to.id]
		sizes[region_to.id] = sizes[region_from.id]

		local_neighbours = []

		for similarity in similarities:
		if similarity.from == p.from or similarity.to == p.from or similarity.from == p.to or similarity.to == p.to:
			from = similarity.to if similarity.from == p.from or similarity.from == p.to else similarity.from
			if from not in local_neighbours:
			local_neighbours.append(from)
			similarity.removed = True

		similarities = [sim for sim in similarities if not sim.removed]

		for local_neighbour in local_neighbours:
		similarities.append(Neighbour(from = len(regions) - 1, to = local_neighbour, similarity = s(regions[n.from].id, regions[n.to].id), removed = False))

	for region in regions:
		region.rank = random.random() * region.level

	return regions

class HandcraftedRegionFeatures:
	def __init__(img, regions, sizes, color_histogram_bins_size = 25, texture_histogram_bins_size = 10):
		nb_segs = int(regions.max()) + 1

		self.img_planes = cv2.split(img)
		self.size_image = img.rows * img.cols
		self.sizes = sizes
		
		self.color_histogram_bins_size = color_histogram_bins_size
		self.color_histogram_size = self.color_histogram_bins_size * img.channels
		self.color_histograms = np.zeros((nb_segs, histogram_size))
		for r in range(nb_segs):
			for p in range(img.channels):
				self.color_histograms[r][p] = cv2.calcHist(img_planes[p], nimages = 1, channels = [0], mask = regions == r, dims = 1, histSize = [self.color_histogram_bins_size], ranges = [0, 256])
			self.color_histograms[r] /= np.sum(self.color_histograms[r])
		
		points = [[] for k in range(nb_segs)]
		for i in range(regions.rows):
			p = regions[i]
			for j in range(regions.cols): 
				points[p[j]].append( (j, i) )
		self.bounding_rects = [cv2.boundingRect(points[seg]) for seg in range(nb_seg)]

		range_ = [0.0, 256.0]
		self.texture_histogram_bins_size = texture_histogram_bins_size
		self.texture_histogram_size = self.texture_histogram_bins_size * img.channels * 8

		self.texture_histograms = np.zeros((nb_segs, histogram_size))

		img_gaussians = []

		for p in range(img.channels):
			# X, no rot
			tmp_gradient = cv2.Scharr(img_planes[p], cv2.CV_32F, 1, 0)
			tmp_gradient_pos, tmp_gradient_neg = [cv2.threshold(tmp_gradient, 0, 0, type)[-1] for type in [cv2.THRESH_TOZERO, cv2.THRESH_TOZERO_INV]]
			img_gaussians.extend([tmp_gradient_pos.clone(), tmp_gradient_neg.clone()])
			
			# Y, no rot
			tmp_gradient = cv2.Scharr(img_planes[p], cv2.CV_32F, 0, 1)
			tmp_gradient_pos, tmp_gradient_neg = [cv2.threshold(tmp_gradient, 0, 0, type)[-1] for type in [cv2.THRESH_TOZERO, cv2.THRESH_TOZERO_INV]]
			img_gaussians.extend([tmp_gradient_pos.clone(), tmp_gradient_neg.clone()])

			center = (img.cols / 2.0, img.rows / 2.0)
			rot = cv2.getRotationMatrix2D(center, 45.0, 1.0)
			bbox = cv2.RotatedRect(center, img.size(), 45.0).boundingRect()
			rot[0, 2] += bbox.width/2.0 - center.x
			rot[1, 2] += bbox.height/2.0 - center.y
			img_plane_rotated = cv2.warpAffine(img_planes[p], rot, bbox.size())

			# X, rot
			tmp_gradient = cv2.Scharr(img_plane_rotated, cv2.CV_32F, 1, 0)
			center = (int(img_plane_rotated.cols / 2.0), int(img_plane_rotated.rows / 2.0))
			rot = cv2.getRotationMatrix2D(center, -45.0, 1.0)
			bbox2 = cv2.RotatedRect(center, img_plane_rotated.size(), -45.0).boundingRect()
			tmp_rot = cv2.warpAffine(tmp_gradient, rot, bbox2.size())


			start_x, start_y = max(0, (bbox.width - img.cols) / 2), max(0, (bbox.height - img.rows) / 2)
			
			tmp_gradient = tmp_rot(Rect(start_x, start_y, img.cols, img.rows))
			tmp_gradient_pos, tmp_gradient_neg = [cv2.threshold(tmp_gradient, 0, 0, type)[-1] for type in [cv2.THRESH_TOZERO, cv2.THRESH_TOZERO_INV]]
			img_gaussians.extend([tmp_gradient_pos.clone(), tmp_gradient_neg.clone()])

			// Y, rot
			tmp_gradient = cv2.Scharr(img_plane_rotated, cv2.CV_32F, 0, 1)
			center = (int(img_plane_rotated.cols / 2.0), int(img_plane_rotated.rows / 2.0))
			rot = cv2.getRotationMatrix2D(center, -45.0, 1.0)
			bbox2 = cv2.RotatedRect(center, img_plane_rotated.size(), -45.0).boundingRect()
			tmp_rot = cv2.warpAffine(tmp_gradient, rot, bbox2.size())

			start_x, start_y = max(0, (bbox.width - img.cols) / 2), max(0, (bbox.height - img.rows) / 2)
			
			tmp_gradient = tmp_rot(Rect(start_x, start_y, img.cols, img.rows))
			tmp_gradient_pos, tmp_gradient_neg = [cv2.threshold(tmp_gradient, 0, 0, type)[-1] for type in [cv2.THRESH_TOZERO, cv2.THRESH_TOZERO_INV]]
			img_gaussians.extend([tmp_gradient_pos.clone(), tmp_gradient_neg.clone()])
		
		for i in range(img.channels() * 8):
			hmin, hmax = img_gaussians[i].min(), img_gaussians[i].max()
			img_gaussians[i] = img_gaussians[i].convertTo(tmp, cv2.CV_8U, (range_[1] - 1) / (hmax - hmin), -(range_[1] - 1) * hmin / (hmax - hmin))
		
		
		totals = [0] * nb_seg
		tmp_histograms = np.zeros((nb_segs, histogram_size), dtype = np.int32)

		for x in range(regions.total):
			region = regions[x]
			histogram = tmp_histograms[region]

			for p in range(img.channels):
				for i in range(8):
					val = int(img_gaussians[p * 8 + i][x])
					bin = int(float(val) / (range[1] / histogram_bins_size))
					histogram[(p * 8 + i) * histogram_bins_size + bin] += 1
					totals[region] += 1

		for r in range(nb_segs):
			for h_pos2 in range(histogram_size):
				histogram[r][h_pos2] = float(tmp_histogram[r][h_pos2]) / float(totals[r])

	def merge(self, r1, r2):
		self.color_histograms[r1] = self.color_histograms[r2] = [ (h1[i] * self.sizes[r1] + h2[i] * self.sizes[r2]) / (self.sizes[r1] + self.sizes[r2]) for i in range(histogram_size) ]
		
		self.texture_histograms[r1] = self.texture_histograms[r2] = [ (self.texture_histograms[r1][i] * self.sizes[r1] + self.texture_histograms[r2][i] * self.sizes[r2]) / (self.sizes[r1] + size_r2) for i in range(self.texture_histogram_size) ]
		
		self.bounding_rects[r1] = self.bounding_rects[r2] = self.bounding_rects[r1] | self.bounding_rects[r2]


	def Size(self, r1, r2):
		return max(min(1.0 - float(self.sizes[r1] + self.sizes[r2]) / float(self.size_image), 1.0), 0.0)
	
	def Color(self, r1, r2):
		return sum(min(self.color_histograms[r1][i], self.color_histograms[r2][i]) for i in range(self.color_histogram_size))

	def Fill(self, r1, r2):
		return max(min(1.0 - float((self.bounding_rects[r1] | self.bounding_rects[r2]).area() - self.sizes[r1] - self.sizes[r2]) / float(self.size_image), 1.0), 0.0)

	def Texture(self, r1, r2):
		return sum(min(self.texture_histograms[r1][i], self.texture_histograms[r2][i]) for i in range(histogram_size))

if __name__ == '__main__':
	base_image = cv2.imread(sys.argv[1])
	regions = selectiveSearchFast(base_image)
	print(regions)
