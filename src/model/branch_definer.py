import torch
import numpy as np
import scipy.ndimage
import tqdm

# Define device
if torch.cuda.is_available():
	DEVICE = "cuda"
else:
	DEVICE = "cpu"


def run_forward_diffusion(
	inputs_by_class, diffuser, times, verbose=True, store_on_gpu=True
):
	"""
	Runs the forward diffusion process over many time points and
	saves the result for each class.
	Arguments:
		`inputs_by_class`: dictionary mapping class to input tensors of shape
			B x D, where B is the batch dimension and D is the input shape (D
			may represent many dimensions here)
		`diffuser`: instantiated diffuser object that operates on input tensors
			of the shape D
		`times`: array of times to perform diffusion at, length T
		`verbose`: whether or not to print out progress updates
		`store_on_gpu`: if False, keep the final results on CPU
	Returns a dictionary mapping class to T x B x D tensors.
	"""
	if store_on_gpu:
		device = "cpu"
	diffused_inputs_by_class = {}
	for c, x0 in inputs_by_class.items():
		if verbose:
			print("Forward-diffusing class %s" % c)
		result = torch.empty((len(times),) + x0.shape, device=device)
		for t_i, t in enumerate(times):
			xt, _ = diffuser.forward(x0, torch.full(x0.shape[:1], t).to(DEVICE))
			result[t_i] = xt.to(device)
		diffused_inputs_by_class[c] = result
	
	return diffused_inputs_by_class


def compute_time_similarities(diffused_inputs_by_class, times, verbose=True):
	"""
	Given the output of `run_forward_diffusion`, computes the average
	similarity between classes at each time point.
	Arguments:
		`diffused_inputs_by_class`: dictionary mapping class to T x B x D
			tensors
		`times`: T-array of times at which diffusion was performed
		`verbose`: whether or not to print out progress updates
	Returns a T x C x C array of similarities between classes at each time, and
	a list of C classes parallel to the ordering in the similarity matrix.
	"""
	classes = list(sorted(diffused_inputs_by_class.keys()))
	sim_matrix = np.empty((len(times), len(classes), len(classes)))
	
	t_iter = tqdm.trange(len(times)) if verbose else range(len(times))
	warning = True
	for t_i in t_iter:
		for i in range(len(classes)):
			for j in range(i + 1):
				inputs_1 = torch.flatten(
					diffused_inputs_by_class[classes[i]][t_i].to(DEVICE),
					start_dim=1
				)
				inputs_2 = torch.flatten(
					diffused_inputs_by_class[classes[j]][t_i].to(DEVICE),
					start_dim=1
				)
				
				if i == j:
					# Flip so we always compare different objects
					inputs_2 = torch.flip(inputs_2, dims=(0,))
					if len(inputs_2) % 2 == 1:
						mid = len(inputs_2) // 2
						temp = inputs_2[mid]
						inputs_2[mid] = inputs_2[0]
						inputs_2[0] = temp
				elif inputs_1.shape[0] != inputs_2.shape[0]:
					# Truncate to the same size
					if inputs_1.shape[0] < inputs_2.shape[0]:
						if warning:
							print("Warning: truncating %d to %d samples" % (
								inputs_2.shape[0], inputs_1.shape[0]
							))
							warning = False
						inputs_2 = inputs_2[:inputs_1.shape[0]]
					else:
						if warning:
							print("Warning: truncating %d to %d samples" % (
								inputs_1.shape[0], inputs_2.shape[0]
							))
							warning = False
						inputs_1 = inputs_1[:inputs_2.shape[0]]
				
				sims = torch.nn.functional.cosine_similarity(
					inputs_1, inputs_2, dim=1
				)
				sim = torch.mean(sims).item()
				sim_matrix[t_i, i, j] = sim
				sim_matrix[t_i, j, i] = sim

	return sim_matrix, classes


def compute_branch_points(
	time_sim_matrix, times, sim_matrix_classes, smooth_sigma=3, epsilon=0.005,
	min_branch_time=0, min_branch_length=0
):
	"""
	Given the output of `compute_time_similarities`, computes the proper branch
	points for the classes in a tree structure.
	Arguments:
		`time_sim_matrix`: T x C x C matrix of similarities of each class to
			each other at various times in the diffusion process
		`times`: T-array of times
		`sim_matrix_classes`: list of C classes in the same order as in
			`time_sim_matrix`
		`smooth_sigma`: width of Gaussian filter to use to smooth each time
			trajectory by
		`epsilon`: error boundary for comparing similarities
		`min_branch_time`: the earliest time possible for a branch point
		`min_branch_length`: the shortest branch length allowed
	Returns a list of branch points, where each branch point is a tuple of a
	time, and tuples of the classes which diverge at that branch point.
	"""
	assert np.all(np.diff(times) > 0)
	
	# Smooth the similarity trajectories over time
	for i in range(time_sim_matrix.shape[1]):
		for j in range(i + 1):
			time_sim_matrix[:, i, j] = scipy.ndimage.gaussian_filter(
				time_sim_matrix[:, i, j], sigma=smooth_sigma
			)
			if i != j:
				time_sim_matrix[:, j, i] = time_sim_matrix[:, i, j]
	
	# For each distinct pair of classes, get the first (earliest) time that the
	# average similarity is about the same as the average similarity within the
	# classes
	crossover_times = np.zeros(time_sim_matrix.shape[1:])
	for i in range(time_sim_matrix.shape[1]):
		for j in range(i):
			intra_sim = (time_sim_matrix[:, i, i] + time_sim_matrix[:, j, j]) \
				/ 2
			inter_sim = time_sim_matrix[:, i, j]
			crossed = np.where(inter_sim >= intra_sim - epsilon)[0]
			if not crossed.size:
				raise ValueError(
					("Index %d and %d intersimilarity" % (i, j)) +
					"did not cross intrasimilarity"
				)
			crossover_time = times[np.min(crossed)]
			crossover_times[i, j] = crossover_time
			crossover_times[j, i] = crossover_time
			
	# Compute the branch points using disjoint sets
	# From the earliest crossover time to the latest, iteratively merge classes
	# until all classes are in the same set
	def union(ds_arr, ds_vals, time, root_1, root_2):
		if ds_arr[root_1] < ds_arr[root_2]:
			# root_2 is larger
			ds_arr[root_2] += ds_arr[root_1]  # Update size
			ds_arr[root_1] = root_2  # root_2 is parent of root_1
			ds_vals[root_2] = (ds_vals[root_1][0] | ds_vals[root_2][0], time)
			ds_vals[root_1] = (None, time)
		else:
			# root_1 is larger or equal
			ds_arr[root_1] += ds_arr[root_2]  # Update size
			ds_arr[root_2] = root_1  # root_1 is parent of root_2
			ds_vals[root_1] = (ds_vals[root_1][0] | ds_vals[root_2][0], time)
			ds_vals[root_2] = (None, time)
	
	def find(ds_arr, x):
		if ds_arr[x] < 0:
			# x is root
			return x
		else:
			# Find root of x's parent and set x's parent to be that root
			ds_arr[x] = find(ds_arr, ds_arr[x])
			return ds_arr[x]
	
	sorted_inds = np.stack(
		np.unravel_index(
			np.argsort(np.ravel(crossover_times)), crossover_times.shape
		),
		axis=1
	)
	ds_arr = np.full(time_sim_matrix.shape[1], -1)
	ds_vals = [(set([sim_matrix_classes[i]]), 0) for i in range(len(ds_arr))]
	# Value of each set is: (members of set, time point of root)
	branch_points = []
	for i, j in sorted_inds:
		if i == j:
			continue
		
		# If i and j are in the same set, move on
		root_i, root_j = find(ds_arr, i), find(ds_arr, j)
		if root_i == root_j:
			continue
		
		# Otherwise, merge together the sets containing i and j
		time_i, time_j = ds_vals[root_i][1], ds_vals[root_j][1]
		branch_time = max(
			min_branch_time if time_i == 0 else time_i + min_branch_length,
			min_branch_time if time_j == 0 else time_j + min_branch_length,
			crossover_times[i, j]
		)
		branch_points.append((
			branch_time,
			tuple(sorted(ds_vals[root_i][0])),
			tuple(sorted(ds_vals[root_j][0]))
		))
		union(ds_arr, ds_vals, branch_time, root_i, root_j)
		
		# If all classes are in the same set, stop
		if np.sum(ds_arr < 0) == 1:
			break
	
	return branch_points


def branch_points_to_branch_defs(branch_points, t_limit, t_start=0):
	"""
	Converts a set of branch points to a different format of branch definitions.
	Arguments:
		`branch_points`: the output of `compute_branch_points`
		`t_limit`: final time point
		`t_start`: initial time point
	Returns a list of branch definitions, where each definition is a tuple of:
	the tuple of classes in that branch, the start time, and the end time.
	"""
	assert np.all(np.diff([bp[0] for bp in branch_points]) >= 0)
	# Branch points are sorted by time, so last point has all classes
	all_classes = tuple(sorted(branch_points[-1][1] + branch_points[-1][2]))
	current_branch_ends = [(t_limit, all_classes)]
	
	branch_defs = []
	for bp in branch_points[::-1]:
		# In reverse order, iterate through branch points
		# Figure out which of the current branch ends it splits
		split_time = bp[0]
		classes = tuple(sorted(bp[1] + bp[2]))
		be_i = [
			i for i, be in enumerate(current_branch_ends) if classes == be[1]
		]
		if len(be_i) != 1:
			raise ValueError("Found %d branch ends matching classes %s" % (
				len(be_i), classes
			))
		be_i = be_i[0]
		
		# The branch it splits is now over, and can be added to branch_defs
		be = current_branch_ends.pop(be_i)  # Remove it from the current set
		branch_defs.append((be[1], bp[0], be[0]))
		
		# The branch point introduces two new branch ends with the same end time
		current_branch_ends.extend([(bp[0], bp[1]), (bp[0], bp[2])])
	
	# The remaining branch ends all have branch starts which are t_start
	for be in current_branch_ends:
		branch_defs.append((be[1], t_start, be[0]))
	
	return branch_defs
