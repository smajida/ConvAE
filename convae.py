# Copyright (c) 2015 ev0
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import time
import random
import numpy as np
import theano as thn
import theano.tensor as tn
import theano.tensor.nnet.conv as conv
import matplotlib.pyplot as plt
import cPickle as cpkl
from theano import shared
from theano.tensor.signal.conv import conv2d as conv2
from theano.tensor.signal.downsample import max_pool_2d as max_pool
from theano.tensor.signal.downsample import max_pool_2d_same_size as max_pool_same
from skimage.transform import downscale_local_mean as downsample
from copy import deepcopy
from util import *


def sigmoid(data):
	"""
	Run The sigmoid activation function over the input data.

	Args:
	----
		data : A k x N array.

	Returns:
	-------
		A k x N array.
	"""
	return 1 / (1 + np.exp(-data))


def softmax(data):
	"""
	Run the softmax activation function over the input data.

	Args:
	----
		data : A k x N array.

	Returns:
	-------
		A k x N array.
	"""
	k, N = data.shape
	e = np.exp(data)
	return e/np.sum(e, axis=0).reshape(1, N)


def sech2(data):
	"""
	Find the hyperbolic secant function over the input data.

	Args:
	-----
		data : A k x N array.

	Returns:
	--------
		A k x N array.
	"""
	return np.square(1 / np.cosh(data))


def relu(data):
	"""
	Perform rectilinear activation on the data.

	Args:
	-----
		data: A k x N array.

	Returns:
	--------
		A k x N array.
	"""
	return np.maximum(data, 0)


def epsilonDecay(eps, phi, satr, itr, intvl):
	"""
	Decay the given learn rate given.

	Args:
	-----
		eps: Learning rate.
		phi: Learning decay.
		satr: Iteration to saturate learning rate or string 'Inf'.
		itr: Current iteration.
		intvl: Decay interval i.e 0 (constant), 1 (progressive) etc.

	Returns:
	--------
		The learning rate to apply.
	"""
	if intvl != 0:
		i = min(itr, float(satr)) / intvl
		return eps / (1.0 + (i * phi))
	else:
		return eps


def fastConv2d(data, kernel, convtype='valid', stride=(1, 1)):
	"""
	Convolve data with the given kernel.

	Args:
	-----
		data: A N x l x m2 x n2 array.
		kernel: An k x l x m1 x n1 array.

	Returns:
	--------
		A N x k x m x n array representing the output.
	"""
	_data, _kernel =  np.asarray(data, dtype='float32'), np.asarray(kernel, dtype='float32')
	d = tn.ftensor4('d')
	k = tn.ftensor4('k')
	f = thn.function([], conv.conv2d(d, k, None, None, convtype, stride), givens={d: shared(_data), k: shared(_kernel)})
	return f()


def strideUpsample(data, stride):
	"""
	Stride image for convolutional upsampling.

	Args:
	-----
		data: An N x k x m x n array of images.
		stride: Tuple repr. stride.

	Returns:
	--------
		An N x k x ((m x stride) - 1) x ((n x stride) - 1) array.
	"""
	kernel = np.zeros(stride)
	kernel[0, 0] = 1

	result = np.kron(data, kernel)
	if stride[0] > 1: # clip last row & column of images
		result = result[:, :, :-(stride[0] - 1), :-(stride[1] - 1)]

	return result


def rot2d90(data, no_rots):
	"""
	Rotate the 2d planes in a 4d array by 90 degrees no_rots times.

	Args:
	-----
		data: A N x k x m x n array.
		no_rots: An integer repr. the no. rotations by 90 degrees.

	Returns:
	--------
		A N x k x m x n array with each m x n plane rotated.
	"""
	# stack, cut & place, rotate, cut & place, break.
	N, k, m, n = data.shape
	result = data.reshape(N * k, m, n)
	result = np.transpose(result, (2, 1, 0))
	result = np.rot90(result, no_rots)
	result = np.transpose(result, (2, 1, 0))
	result = result.reshape(N, k, m, n)

	return result


def maxpool(data, factor, getPos=True):
	"""
	Return max pooled data and the pooled pixel positions.

	Args:
	-----
		data: An N x k x m x n array.
		factor: Pooling factor.

	Returns:
	--------
		An N x k x (m/factor) x (n/factor), N x k x m x n arrays.
	"""
	_data = np.asarray(data, dtype='float32')
	x = tn.ftensor4('x')
	f = thn.function([], max_pool(x, factor, True), givens={x: shared(_data)})
	g = thn.function([], max_pool_same(x, factor)/x, givens={x: shared(_data + 0.0000000001)})

	pooled = f()
	if not getPos:
		return pooled

	positions = g()
	positions[np.where(np.isnan(positions))] = 0
	return pooled, positions



class PoolLayer():
	"""
	Pooling layer class.
	"""

	def __init__(self, factor, poolType='avg', decode=False):
		"""
		Initialize pooling layer.

		Args:
		-----
			factor: Tuple repr. pooling factor.
			poolType: String repr. the pooling type i.e 'avg' or 'max'.
			decode: Boolean indicator if layer is encoder or decoder.
		"""
		self.type, self.factor, self.positions, self.decode = poolType, factor, None, decode


	def bprop(self, dEdo):
		"""
		Compute error gradients and return sum of error from output down 
		to this layer.

		Args:
		-----
			dEdo: A N x l x m2 x n2 array of errors from prev layers.

		Returns:
		--------
			A N x k x x m1 x m1 array of errors.
		"""
		if self.decode:
			dE = downsample(dEdo, (1, 1, self.factor[0], self.factor[1])) * np.sum(self.factor)
		else:
			if self.type == 'max':
				dE = np.kron(dEdo, np.ones(self.factor)) * self.positions
			else:
				dE = np.kron(dEdo, np.ones(self.factor)) * (1.0 / np.sum(self.factor))

		return dE
			

	def update(self, eps_w, eps_b, mu, l2, useRMSProp, RMSProp_decay, minsq_RMSProp):
		"""
		Update the weights in this layer.

		Args:
		-----
			eps_w, eps_b: Learning rates for the weights and biases.
			mu: Momentum coefficient.
			l2: L2 Regularization coefficient.
			useRMSProp: Boolean indicating the use of RMSProp.
		"""
		pass #Nothing to do here :P


	def feedf(self, data):
		"""
		Pool features within a given receptive from the input data.

		Args:
		-----
			data: An N x k x m1 x n1 array of input plains.

		Returns:
		-------
			A N x k x m2 x n2 array of output plains.
		"""
		if self.decode:
			if self.type == 'max':
				pooled = np.kron(data, np.ones(self.factor))
			else:
				pooled = np.kron(data, np.ones(self.factor)) * (1.0 / np.sum(self.factor))
		else:
			if self.type == 'max':
				pooled, self.positions = maxpool(data, self.factor)
			else:
				pooled = downsample(data, (1, 1, self.factor[0], self.factor[1]))

		return pooled


class ConvLayer():
	"""
	Convolutional layer class.
	"""

	def __init__(self, noKernels, channels, kernelSize, outputType='relu', stride=1, init_w=0.01, init_b=0, decode=False):
		"""
		Initialize convolutional layer.

		Args:
		-----
			noKernels: No. feature maps in layer.
			channels: No. input planes in layer or no. channels in input image.
			kernelSize: Tuple repr. the size of a kernel.
			stride: Integer repr. convolutional stride.
			outputType: String repr. type of non-linear activation i.e 'relu', 'tanh' or 'sigmoid'.
			init_w: Std dev of initial weights drawn from a std Normal distro.
			init_b: Initial value of biases.
			decode: Boolean indicator whether layer is encoder or decoder.
		"""
		self.o_type = outputType
		self.init_w, self.init_b = init_w, init_b
		self.kernels = self.init_w * np.random.randn(noKernels, channels, kernelSize[0], kernelSize[1])
		self.bias = self.init_b * np.ones((noKernels, 1, 1))
		self.stride = stride, stride
		self.v_w, self.dw_ms, self.v_b, self.db_ms = 0, 0, 0, 0
		self.decode = decode


	def bprop(self, dEdo):
		"""
		Compute error gradients and return sum of error from output down
		to this layer.

		Args:
		-----
			dEdo: A N x k x m2 x n2 array of errors from prev layers.

		Returns:
		-------
			A N x l x m1 x n1 array of errors.
		"""
		if self.o_type == 'sigmoid':
			theta = sigmoid(self.maps + self.bias)
			dEds = dEdo * theta * (1 - theta)
		elif self.o_type == 'tanh':
			dEds = dEdo * sech2(self.maps + self.bias)
		else:
			dEds = dEdo * np.where((self.maps + self.bias) > 0, 1, 0)

		if not self.decode:
			dEds = strideUpsample(dEds, self.stride)

		self.dEdb = np.sum(np.sum(np.average(dEds, axis=0), axis=1), axis=1).reshape(self.bias.shape)

		# correlate.
		xs, dEds = np.swapaxes(self.x, 0, 1), np.swapaxes(dEds, 0, 1)
		if self.decode:
			self.dEdw = fastConv2d(dEds, rot2d90(xs, 2)) / dEdo.shape[0]
		else:	
			self.dEdw = fastConv2d(xs, rot2d90(dEds, 2)) / dEdo.shape[0]
			self.dEdw = np.swapaxes(self.dEdw, 0, 1)
			self.dEdw = rot2d90(self.dEdw, 2)

		# correlate
		dEds, kernels = np.swapaxes(dEds, 0, 1), np.swapaxes(self.kernels, 0, 1)
		if self.decode:
			return fastConv2d(dEds, rot2d90(kernels, 2), stride=self.stride)
		else:
			return fastConv2d(dEds, rot2d90(kernels, 2), 'full')


	def update(self, eps_w, eps_b, mu, l2, useRMSProp, RMSProp_decay, minsq_RMSProp):
		"""
		Update the weights in this layer.

		Args:
		-----
			eps_w, eps_b: Learning rates for the weights and biases.
			mu: Momentum coefficient.
			l2: L2 Regularization coefficient.
			useRMSProp: Boolean indicating the use of RMSProp.
			RMSProp_decay: Decay term for the squared average.
			minsq_RMSProp: Constant added to square-root of squared average. 
		"""
		if useRMSProp:
			self.dw_ms = (RMSProp_decay * self.dw_ms) + ((1.0 - RMSProp_decay) * np.square(self.dEdw))
			self.db_ms = (RMSProp_decay * self.db_ms) + ((1.0 - RMSProp_decay) * np.square(self.dEdb))
			self.dEdw = self.dEdw / (np.sqrt(self.dw_ms) + minsq_RMSProp)
			self.dEdb = self.dEdb / (np.sqrt(self.db_ms) + minsq_RMSProp)
			self.dEdw[np.where(np.isnan(self.dEdw))] = 0
			self.dEdb[np.where(np.isnan(self.dEdb))] = 0

		self.v_w = (mu * self.v_w) - (eps_w * self.dEdw) - (eps_w * l2 * self.kernels)
		self.v_b = (mu * self.v_b) - (eps_b * self.dEdb) - (eps_b * l2 * self.bias)
		self.kernels = self.kernels + self.v_w
		self.bias = self.bias + self.v_b


	def feedf(self, data):
		"""
		Return the non-linear result of convolving the input data with the
		weights in this layer.

		Args:
		-----
			data: An N x l x m1 x n1 array of input plains.

		Returns:
		-------
			A N x k x m2 x n2 array of output plains.
		"""
		if self.decode:
			self.x = strideUpsample(data, self.stride)
			self.maps = fastConv2d(self.x, self.kernels, 'full')
		else:
			self.x = data	
			self.maps = fastConv2d(self.x, self.kernels, stride=self.stride)

		if self.o_type == 'tanh':
			return np.tanh(self.maps + self.bias)
		elif self.o_type == 'sigmoid':
			return sigmoid(self.maps + self.bias)

		return relu(self.maps + self.bias)


class ConvAE():
	"""
	Convolutional Autoencoder class.
	"""

	def __init__(self, layers=[]):
		"""
		Initialize autoencoder.

		Args:
		-----
			layers: List of convolutional and pooling layers arranged heirarchically.
		"""
		self.layers, self.encodeInd = [], 0

		if layers != []:

			for ind in xrange(len(layers) - 1, -1, -1):
				self.layers = self.layers + self.reflect(layers[ind])

			self.layers = self.layers + deepcopy(layers)
			self.encodeInd = len(layers)


	def train(self, data, test, params):
		"""
		Train autoencoder to learn a compressed feature representation of data using
		the given params.

		Args:
		-----
			data : A no_imgs x img_length x img_width x no_channels array of images.
			test : A no_imgs x img_length x img_width x no_channels array of images.
			params: A dictionary of training parameters.
		"""

		print "Training network..."
		plt.ion()
		N, itrs, errors = data.shape[0], 0, []

		for epoch in xrange(params['epochs']):

			avg_errors = []
			start, stop = range(0, N, params['batch_size']), range(params['batch_size'], N, params['batch_size'])

			for i, j in zip(start, stop):
 
				error = self.reconstruct(data[i:j]) - data[i:j] # euclidean dist.
				self.backprop(error)
				self.update(params, itrs)

				avg_error = np.average(error)
				print '\r| Epoch: {:5d}  |  Iteration: {:8d}  |  Avg Reconstruction Error: {:.2f}|'.format(epoch, itrs, avg_error)
				if epoch != 0 and epoch % 100 == 0:
  					print '---------------------------------------------------------------------------'

  				itrs = itrs + 1
  				avg_errors.append(avg_error)

  			i = start[-1]
  			recon = self.reconstruct(data[i:]) # to display
  			error = recon - data[i:]
			self.backprop(error)
			self.update(params, itrs)

			avg_error = np.average(error)
			print '\r| Epoch: {:5d}  |  Iteration: {:8d}  |  Avg Reconstruction Error: {:.2f} |'.format(epoch, itrs, avg_error)
			if epoch != 0 and epoch % 100 == 0:
  				print '----------------------------------------------------------------------------'

  			itrs = itrs + 1
  			avg_errors.append(avg_error)

  			# plotting sturvs
  			plt.figure(2)
  			plt.show()
  			errors.append(np.average(avg_errors))
  			plt.xlabel('Epochs')
  			plt.ylabel('Reconstruction Error')
  			plt.plot(range(epoch + 1), errors, '-g')
  			plt.axis([0, params['epochs'], -255, 255])
  			plt.draw()
  			if params['view_kernels']:
  				self.displayKernels()
  			if params['view_recon']:
  				self.display(recon[0 : params['no_images']], 3)
  				self.display(data[i : i + params['no_images']], 4)

		plt.ioff()

		recon = self.reconstruct(test)
		print '\rAverage Reconstruction Error on test images: ', np.average(recon - test)
		self.display(recon[0 : params['no_images']], 5)
		self.display(test[0 : params['no_images']], 6)

		raw_input("Training complete. Press any key to continue.")
  		print "Saving model..."
  		self.saveModel('convaeModel')

  		print "Done."


	def backprop(self, dE):
		"""
		Propagate the error gradients through the network.

		Args:
		-----
			dE: A no_imgs x img_length x img_width x img_channels array.
		"""
		error = np.transpose(dE, (0, 3, 1, 2))
		for layer in self.layers:
			error = layer.bprop(error)


	def reconstruct(self, imgs):
		"""
		Reconstruct the imgs from codings.

		Args:
		-----
			imgs: A no_imgs x img_length x img_width x img_channels array.

		Returns:
		-------
			A no_imgs x img_length x img_width x img_channels array.
		"""
		data = np.transpose(imgs, (0, 3, 1, 2))

		for i in xrange(len(self.layers) - 1, - 1, -1):
			data = self.layers[i].feedf(data)

		return np.transpose(data, (0, 2, 3, 1))


	def update(self, params, i):
		"""
		Update the network weights.

		Args:
		-----
			params: Training parameters.
		"""
		eps_w = epsilonDecay(params['eps_w'], params['eps_decay'], params['eps_satr'], i, params['eps_intvl'])
		eps_b = epsilonDecay(params['eps_b'], params['eps_decay'], params['eps_satr'], i, params['eps_intvl'])

		for layer in self.layers:
			layer.update(eps_w, eps_b, params['mu'], params['l2'], params['RMSProp'], params['RMSProp_decay'], params['minsq_RMSProp'])


	def saveModel(self, filename):
		"""
		Save the current network model in file filename.

		Args:
		-----
			filename: String repr. name of file.
		"""
		model = {
			'network': self.layers,
			'index': self.encodeInd
		}

		f = open(filename, 'w')
		cpkl.dump(model, f, 1)
		f.close()


	def loadModel(self, filename):
		"""
		Load an empty architecture with the network model
		saved in file filename.

		Args:
		-----
			filename: String repr. name of file.
		"""
		f = open(filename, 'r')
		model = cpkl.load(f)

		if model != {} and self.layers == []:
			self.layers = model["network"]
			self.encodeInd = model["index"]

		f.close()


	def displayKernels(self):
		"""
		Displays the kernels in the first layer.
		"""

		kernels = self.layers[len(self.layers) - 1].kernels
		
		if kernels.shape[1] == 2 or kernels.shape[1] > 4:
			print "displayKernels() Error: Invalid number of channels."
			pass

		kernels = np.transpose(kernels, (0, 3, 2, 1))
		self.display(kernels, 1)


	def display(self, imgs, f):
		"""
		Display the given images.

		Args:
		-----
			imgs: Images to display.
			f: Figure no. on which to display images.
		"""
		N, m, n, c = imgs.shape

		x = np.ceil(np.sqrt(N))
		y = np.ceil(N / x)

		plt.figure(f)
		for i in xrange(N):
			plt.subplot(x, y, i)
			img = imgs[i]
			if img.shape[2] == 1:
				plt.imshow(img[:, :, 0], 'gray')
			else:
				plt.imshow(img)
			plt.axis('off')

		plt.draw()


	def reflect(self, layer):
		"""
		Get a reflected copy of the given encoding layer.

		Args:
		-----
			layer: An encoding layer.

		Returns:
		--------
			A single element array containing the decoding layer.
		"""

		if isinstance(layer, ConvLayer):
			k = layer.kernels.shape
			return [ConvLayer(k[1], k[0], (k[2], k[3]), layer.o_type, layer.stride[0], layer.init_w, layer.init_b, True)]
		elif isinstance(layer, PoolLayer):
			return [PoolLayer(layer.factor, layer.type, True)]


def testMnist():
	"""
	Test autoencoder on MNIST dataset.
	"""

	print "Loading MNIST images..."
	data = np.load('data/mnist.npz')
	train_data = data['train_data'][0:45000].reshape(45000, 28, 28, 1)
	valid_data = data['valid_data'][0:5000].reshape(5000, 28, 28, 1)
	train_data = np.concatenate((train_data, valid_data))
	test_data = data['test_data'].reshape(10000, 28, 28, 1)

	print "Creating network..."

	layers = [
				PoolLayer((2, 2), 'max'),
				ConvLayer(6, 1, (7, 7), stride=3)
			]

	params = {
		'epochs': 30,
		'batch_size': 500,
		'view_kernels': False,
		'view_recon': True,
		'no_images': 5,
		'eps_w': 0.0007,
		'eps_b': 0.0007,
		'eps_decay': 9,
		'eps_intvl': 30,
		'eps_satr': 'inf',
		'mu': 0.7,
		'l2': 0.95,
		'RMSProp': True,
		'RMSProp_decay': 0.9,
		'minsq_RMSProp': 0,
	}

	ae = ConvAE(layers)
	ae.train(train_data, test_data, params)


def testTorontoFaces():
	"""
	Test autoencoder on Toronto Faces dataset.
	"""

	print "Loading Toronto Facial images..."
	data = np.load('data/faces.npz')
	train_data = np.transpose(data['train_data'], (2, 0, 1)).reshape(2925, 32, 32, 1)
	test_data = np.transpose(data['test_data'], (2, 0, 1)).reshape(418, 32, 32, 1)

	print "Creating network..."

	layers = [
				PoolLayer((2, 2), 'max'),
				ConvLayer(6, 1, (3, 3))
			]

	params = {
		'epochs': 50,
		'batch_size': 500,
		'view_kernels': False,
		'view_recon': True,
		'no_images': 12,
		'eps_w': 0.005,
		'eps_b': 0.005,
		'eps_decay': 9,
		'eps_intvl': 10,
		'eps_satr': 'inf',
		'mu': 0.7,
		'l2': 0.95,
		'RMSProp': True,
		'RMSProp_decay': 0.9,
		'minsq_RMSProp': 0.01,
	}

	ae = ConvAE(layers)
	ae.train(train_data, test_data, params)



if __name__ == '__main__':

	#testMnist()
	testTorontoFaces()