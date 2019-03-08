from __future__ import print_function

import os
import h5py
import multiprocessing 
import re
import argparse
import json
import sys
import random
import numpy as np
import networkx as nx
import pandas as pd

from scipy.sparse import identity
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from sklearn.metrics.pairwise import cosine_similarity, euclidean_distances

from utils import build_training_samples, hyperboloid_to_poincare_ball, load_data
from utils import perform_walks, determine_positive_and_negative_samples
from losses import  hyperbolic_softmax_loss
from generators import TrainingDataGenerator
from visualise import draw_graph

from keras.layers import Input, Layer, Dense, Embedding
from keras.models import Model
from keras import backend as K
# from keras.callbacks import Callback, TerminateOnNaN, TensorBoard, ModelCheckpoint, CSVLogger, EarlyStopping

import tensorflow as tf
from tensorflow.python.framework import ops
from tensorflow.python.ops import math_ops, control_flow_ops
from tensorflow.python.training import optimizer

K.set_floatx("float64")
K.set_epsilon(1e-15)

np.set_printoptions(suppress=True)


# TensorFlow wizardry
config = tf.ConfigProto()

# Don't pre-allocate memory; allocate as-needed
config.gpu_options.allow_growth = True
 
# Only allow a total of half the GPU memory to be allocated
config.gpu_options.per_process_gpu_memory_fraction = 0.5

config.log_device_placement=False
config.allow_soft_placement=True

# Create a session with the above options specified.
K.tensorflow_backend.set_session(tf.Session(config=config))

def gans_to_hyperboloid(x):
	t = K.sqrt(1. + K.sum(K.square(x), axis=-1, keepdims=True))
	return tf.concat([x, t], axis=-1)

def euclidean_dot(x, y):
    axes = len(x.shape) - 1, len(y.shape) -1
    return K.batch_dot(x, y, axes=axes)

def minkowski_dot(x, y):
    axes = len(x.shape) - 1, len(y.shape) -1
    return K.batch_dot(x[...,:-1], y[...,:-1], axes=axes) - K.batch_dot(x[...,-1:], y[...,-1:], axes=axes)

def minkowski_dot_2d(x, y):
	# axes = len(x.shape) - 1, len(y.shape) -1
	return K.dot( x[...,:-1], K.transpose(y[...,:-1])) - K.dot(x[...,-1:], K.transpose(y[...,-1:]))

def hyperbolic_distance(x, y):
	inner_uv = minkowski_dot_2d(x, y)
	inner_uv = -inner_uv - 1. + 1e-7
	# inner_uv = K.maximum(inner_uv, K.epsilon()) # clip to avoid nan

	d_uv = tf.acosh(1. + inner_uv)
	return d_uv

def hyperboloid_initializer(shape, r_max=1e-3):

	def poincare_ball_to_hyperboloid(X, append_t=True):
		x = 2 * X
		t = 1. + K.sum(K.square(X), axis=-1, keepdims=True)
		if append_t:
			x = K.concatenate([x, t], axis=-1)
		return 1 / (1. - K.sum(K.square(X), axis=-1, keepdims=True)) * x

	def sphere_uniform_sample(shape, r_max):
		num_samples, dim = shape
		X = tf.random_normal(shape=shape, dtype=K.floatx())
		X_norm = K.sqrt(K.sum(K.square(X), axis=-1, keepdims=True))
		U = tf.random_uniform(shape=(num_samples, 1), dtype=K.floatx())
		return r_max * U ** (1./dim) * X / X_norm

	# w = sphere_uniform_sample(shape, r_max=r_max)
	w = tf.random_uniform(shape=shape, minval=-r_max, maxval=r_max, dtype=K.floatx())
	return poincare_ball_to_hyperboloid(w)

class EmbeddingLayer(Layer):
	
	def __init__(self, 
		num_nodes, 
		embedding_dim, 
		**kwargs):
		super(EmbeddingLayer, self).__init__(**kwargs)
		self.num_nodes = num_nodes
		self.embedding_dim = embedding_dim

	def build(self, input_shape):
		# Create a trainable weight variable for this layer.
		self.embedding = self.add_weight(name='embedding', 
		  shape=(self.num_nodes, self.embedding_dim),
		  initializer=hyperboloid_initializer,
		  trainable=True)
		super(EmbeddingLayer, self).build(input_shape)


	def call(self, x):

		embedding = tf.gather(self.embedding, x)

		return embedding

	def compute_output_shape(self, input_shape):
		return (input_shape[0], input_shape[1], self.embedding_dim+1)
	
	def get_config(self):
		base_config = super(EmbeddingLayer, self).get_config()
		return base_config.update({"num_nodes": self.num_nodes, "embedding_dim": self.embedding_dim})

class ExponentialMappingOptimizer(optimizer.Optimizer):
	
	def __init__(self, 
		learning_rate=0.1, 
		use_locking=False,
		name="ExponentialMappingOptimizer", 
		max_norm=np.inf):
		super(ExponentialMappingOptimizer, self).__init__(use_locking, name)
		self._lr = learning_rate
		self.max_norm = max_norm

	def _prepare(self):
		self._lr_t = ops.convert_to_tensor(self._lr, name="learning_rate", dtype=K.floatx())

	def _apply_dense(self, grad, var):
		assert False
		lr_t = math_ops.cast(self._lr_t, var.dtype.base_dtype)
		spacial_grad = grad[:,:-1]
		t_grad = -1 * grad[:,-1:]
		
		ambient_grad = tf.concat([spacial_grad, t_grad], axis=-1)
		tangent_grad = self.project_onto_tangent_space(var, ambient_grad)
		
		exp_map = self.exponential_mapping(var, - lr_t * tangent_grad)
		
		return tf.assign(var, exp_map)
		
	def _apply_sparse(self, grad, var):
		indices = grad.indices
		values = grad.values

		p = tf.gather(var, indices, name="gather_apply_sparse")

		lr_t = math_ops.cast(self._lr_t, var.dtype.base_dtype)
		spacial_grad = values[:, :-1]
		t_grad = - values[:, -1:]

		ambient_grad = tf.concat([spacial_grad, t_grad], axis=-1, name="optimizer_concat")
		tangent_grad = self.project_onto_tangent_space(p, ambient_grad)
		exp_map = self.exponential_mapping(p, - lr_t * tangent_grad)

		out = tf.scatter_update(ref=var, updates=exp_map, indices=indices, name="scatter_update")

		return out
	
	def project_onto_tangent_space(self, hyperboloid_point, minkowski_ambient):
		tang = minkowski_ambient + minkowski_dot(hyperboloid_point, minkowski_ambient) * hyperboloid_point
		return tang
   
	def exponential_mapping( self, p, x ):

		def adjust_to_hyperboloid(x):
			x = x[:,:-1]
			t = K.sqrt(1. + K.sum(K.square(x), axis=-1, keepdims=True))
			return tf.concat([x, t], axis=-1)

		norm_x = K.sqrt(  minkowski_dot(x, x) ) 
		clipped_norm_x = K.minimum(norm_x, self.max_norm)
		#####################################################
		z = x / K.maximum(norm_x, K.epsilon()) # unit norm 
		exp_map = tf.cosh(clipped_norm_x) * p + tf.sinh(clipped_norm_x) * z
		#####################################################
		exp_map = adjust_to_hyperboloid(exp_map) # account for floating point inprecision

		return exp_map

def build_model(num_nodes, args):

	x = Input(shape=(1 + 1 + args.num_negative_samples,), 
		name="model_input", dtype=tf.int64)
	y = EmbeddingLayer(num_nodes, args.embedding_dim, name="embedding_layer")(x)
	model = Model(x, y)

	initial_epoch = 0

	return model, initial_epoch

def load_embedding(filename):
	with h5py.File(filename, 'r') as f:
		embedding = np.array(f.get("embedding_layer/embedding_layer/embedding:0"))
	return embedding

def parse_args():
	'''
	parse args from the command line
	'''
	parser = argparse.ArgumentParser(description="HEAT algorithm for feature learning on complex networks")

	# parser.add_argument("--data-directory", dest="data_directory", type=str, default="/data/",
	# 	help="The directory containing data files (default is '/data/').")

	# parser.add_argument("--dataset", dest="dataset", type=str, default="cora_ml",
	# 	help="The name of dataset (to be used when saving")

	parser.add_argument("--edgelist", dest="edgelist", type=str, default="datasets/cora_ml/edgelist.tsv",
		help="edgelist to load.")
	parser.add_argument("--features", dest="features", type=str, default="datasets/cora_ml/feats.csv",
		help="features to load.")
	parser.add_argument("--labels", dest="labels", type=str, default="datasets/cora_ml/labels.csv",
		help="path to labels")


	parser.add_argument("--seed", dest="seed", type=int, default=0,
		help="Random seed (default is 0).")
	parser.add_argument("--lr", dest="lr", type=float, default=3e-1,
		help="Learning rate (default is 3e-1).")

	parser.add_argument("-e", "--num_epochs", dest="num_epochs", type=int, default=5,
		help="The number of epochs to train for (default is 5).")
	parser.add_argument("-b", "--batch_size", dest="batch_size", type=int, default=32, 
		help="Batch size for training (default is 32).")
	parser.add_argument("--nneg", dest="num_negative_samples", type=int, default=10, 
		help="Number of negative samples for training (default is 10).")
	parser.add_argument("--context-size", dest="context_size", type=int, default=3,
		help="Context size for generating positive samples (default is 3).")
	parser.add_argument("--patience", dest="patience", type=int, default=300,
		help="The number of epochs of no improvement in validation loss before training is stopped. (Default is 300)")


	parser.add_argument("-d", "--dim", dest="embedding_dim", type=int,
		help="Dimension of embeddings for each layer (default is 2).", default=2)

	parser.add_argument("-p", dest="p", type=float, default=1.,
		help="node2vec return parameter (default is 1.).")
	parser.add_argument("-q", dest="q", type=float, default=1.,
		help="node2vec in-out parameter (default is 1.).")
	parser.add_argument('--num-walks', dest="num_walks", type=int, default=10, 
		help="Number of walks per source (default is 10).")
	parser.add_argument('--walk-length', dest="walk_length", type=int, default=80, 
		help="Length of random walk from source (default is 80).")

	parser.add_argument("--sigma", dest="sigma", type=float, default=1,
		help="Width of gaussian (default is 1).")


	parser.add_argument("--alpha", dest="alpha", type=float, default=0, 
		help="Probability of randomly jumping to a similar node when walking.")

	parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", 
		help="Use this flag to set verbosity of training.")
	parser.add_argument('--workers', dest="workers", type=int, default=2, 
		help="Number of worker threads to generate training patterns (default is 2).")

	# parser.add_argument("--plot", dest="plot_path", default="plots/", 
	# 	help="path to save plots (default is 'plots/)'.")
	# parser.add_argument("--logs", dest="log_path", default="logs/", 
	# 	help="path to save logs (default is 'logs/)'.")
	parser.add_argument("--walks", dest="walk_path", default="walks/cora_ml", 
		help="path to save random walks (default is 'walks/cora_ml)'.")
	# parser.add_argument("--samples", dest="samples_path", default="samples/", 
	# 	help="path to save positive/negative samples (default is 'samples/)'.")
	# parser.add_argument("--model", dest="model_path", default="models/", 
	# 	help="path to save model after each epoch (default is 'models/)'.")
	# parser.add_argument("--test-results", dest="test_results_path", default="test_results/", 
	# 	help="path to save test results (default is 'test_results/)'.")

	parser.add_argument("--embedding", dest="embedding_path", default="embeddings/cora_ml", 
		help="path to save embedings (default is 'embeddings/cora_ml/)'.")

	# parser.add_argument('--evaluate-class-prediction', action="store_true", help='flag to evaluate class prediction')
	# parser.add_argument('--evaluate-link-prediction', action="store_true", help='flag to evaluate link prediction')

	parser.add_argument('--directed', action="store_true", help='flag to train on directed graph')

	parser.add_argument('--use-generator', action="store_true", help='flag to train using a generator')

	parser.add_argument('--visualise', action="store_true", 
		help='flag to visualise embedding (embedding_dim must be 2)')


	args = parser.parse_args()
	return args

def configure_paths(args):
	'''
	build directories on local system for output of model after each epoch
	'''

	directory = os.path.join("alpha={:.02f}".format(args.alpha),
		"seed={:03d}".format(args.seed))
	

	args.walk_path = os.path.join(args.walk_path, directory)
	args.embedding_path = os.path.join(args.embedding_path, directory, "dim={:03d}".format(args.embedding_dim) )

	# assert os.path.exists(args.walk_path)
	if not os.path.exists(args.walk_path):
		os.makedirs(args.walk_path)
		print ("making {}".format(args.walk_path))
	print ("saving walks to {}".format(args.walk_path))

	if not os.path.exists(args.embedding_path):
		os.makedirs(args.embedding_path)
		print ("making {}".format(args.embedding_path))
	print ("saving embedding to {}".format(args.embedding_path))

	# walk filename 
	args.walk_filename = os.path.join(args.walk_path, "num_walks={}-walk_len={}-p={}-q={}.walk".format(args.num_walks, 
				args.walk_length, args.p, args.q))
	# embedding filename
	args.embedding_filename = os.path.join(args.embedding_path, "embedding.csv")
	
def main():

	print ("Beginning execution")

	args = parse_args()

	assert not (args.visualise and args.embedding_dim > 2), "Can only visualise two dimensions"

	random.seed(args.seed)
	np.random.seed(args.seed)
	tf.set_random_seed(args.seed)

	graph, features, node_labels = load_data(args)

	core = graph.subgraph([n for n in graph.nodes() if node_labels[n] == 0]) 
	periphery_in = graph.subgraph([n for n in graph.nodes() if node_labels[n] == 1])
	periphery_out = graph.subgraph([n for n in graph.nodes() if node_labels[n] == 2])

	# for n1 in periphery_in.nodes():
	# 	for n2 in periphery_out.nodes():
	# 		assert not (n1, n2) in graph.edges(), (n1, n2)
	# 		assert not (n2, n1) in graph.edges(), (n2, n1, labels[n1], labels[n2])
	# print("passed")

	print ("Loaded dataset")

	configure_paths(args)

	print ("Configured paths")

	if args.directed:
		directed_edges = graph.edges()
		print ("DISCOVERED {} DIRECTED EDGES".format(len(directed_edges)))
	else:
		directed_edges = None

	graph = graph.to_undirected() # we perform walks on undirected matrix

	# original edges for reconstruction
	undirected_edges = graph.edges()

	# build model
	num_nodes = len(graph)
	model, initial_epoch = build_model(num_nodes, args)

	optimizer = ExponentialMappingOptimizer(learning_rate=args.lr)
	loss = hyperbolic_softmax_loss(sigma=args.sigma)
	model.compile(optimizer=optimizer, loss=loss, 
		target_tensors=[tf.placeholder(dtype=np.int64)])
	model.summary()

	callbacks = []			

	walks = perform_walks(graph, features, args)

	positive_samples, negative_samples, alias_dict =\
		determine_positive_and_negative_samples(nodes=graph.nodes(), 
		walks=walks, context_size=args.context_size, directed=args.directed)

	random.shuffle(positive_samples)

	if not args.use_generator:
		train_x = build_training_samples(np.array(positive_samples), 
				negative_samples,
				args.num_negative_samples, 
				alias_dict)
		train_y = np.zeros(len(train_x), dtype=np.int64)

	if args.use_generator:
		print ("Training with data generator with {} worker threads".format(args.workers))
		training_generator = TrainingDataGenerator(positive_samples,  
				negative_samples, alias_dict, args)

		model.fit_generator(training_generator, 
			workers=args.workers,
			max_queue_size=10, 
			use_multiprocessing=args.workers>0, 
			epochs=args.num_epochs, 
			initial_epoch=initial_epoch, 
			verbose=args.verbose,
			callbacks=callbacks
		)

	else:
		print ("Training without data generator")

		model.fit(train_x, train_y, 
			batch_size=args.batch_size, 
			epochs=args.num_epochs, 
			initial_epoch=initial_epoch, 
			verbose=args.verbose,
			callbacks=callbacks
		)

	embedding = model.get_weights()[-1]
	print ("Training complete, saving embedding to {}".format(args.embedding_path))

	embedding_df = pd.DataFrame(embedding, index=graph.nodes())
	embedding_df.to_csv(args.embedding_filename)

	if args.visualise:
		embedding = hyperboloid_to_poincare_ball(embedding)
		draw_graph(undirected_edges, embedding, node_labels, path="test.png")

if __name__ == "__main__":
	main()