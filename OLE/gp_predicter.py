# Here we will program the gp_predicter class. One instance of this class will be created for each quantity that is to be emulated.
# Each quantity may have different number of dimensions. Thus, each gp_predicter requires a different preprocessing and potentially PCA data compression.
# 
import jax
# use GPJax to fit the data
#from jax.config import config
import os

jax.config.update("jax_enable_x64", True)

from jax import jit, random
import jax.numpy as jnp
import numpy as np
from jaxtyping import install_import_hook
import optax as ox

from functools import partial
import gc
import copy

from jax import grad

with install_import_hook("gpjax", "beartype.beartype"):
    import gpjax as gpx

import time

# Path: OLE/gp_predicter.py

from OLE.utils.base import BaseClass
from OLE.data_processing import data_processor
from OLE.plotting import plain_plot, plain_scatter, loss_plot, plot_pca_components_test_set, plot_prediction_test

from OLE.interfaces import gpjax_interface


class GP_predictor(BaseClass):

    quantity_name: str

    hyperparameters: dict

    GPs: list

    def __init__(self, quantity_name=None, **kwargs):
        super().__init__('GP '+quantity_name, **kwargs)
        self.quantity_name = quantity_name

    def initialize(self, ini_state, **kwargs):
        # in the ini_state and example state is given which contains the parameters and the quantities which are to be emulated. The example state also contains an example value for each quantity which is used to determine the dimensionality of the quantity.

        self.GPs = []

        # default hyperparameters
        defaulthyperparameters = {
            'kernel': 'RBF',

            # plotting directory
            'plotting_directory': None,

            # testset fraction. If we have a testset, which is not None, then we will use this fraction of the data as a testset
            'testset_fraction': None,

            # acceptable error
            'error_tolerance': 0.1

        }

        # The hyperparameters are a dictionary of the hyperparameters for the different quantities. The keys are the names of the quantities.
        self.hyperparameters = defaulthyperparameters

        for key, value in kwargs.items():
            self.hyperparameters[key] = value

        # We need to determine the dimensionality of the quantity to be emulated. This is done by looking at the example state.
        self.output_size = len(ini_state['quantities'][self.quantity_name])
        self.info('Output size: %d', self.output_size)

        # We need to determine the dimensionality of the parameters. This is done by looking at the example state.
        self.input_size = len(ini_state['parameters'])
        self.info('Input size: %d', self.input_size)

        # We can now initialize the data processor for this quantity.
        self.data_processor = data_processor('Data processor ' + self.quantity_name, debug=self.debug_mode)
        self.data_processor.initialize(self.input_size, self.output_size, self.quantity_name,**kwargs)

        # For each dimension of the output_data we create a GP.
        self.GPs = []
        self.num_GPs = 0 # the number of GPs we have

        pass

    # @partial(jit, static_argnums=0) 
    def predict(self, parameters):
        # Predict the quantity for the given parameters.
        # First we normalize the parameters.
        parameters_normalized = self.data_processor.normalize_input_data(parameters)

        # Then we predict the output data for the normalized parameters.
        output_data_compressed = jnp.zeros(self.num_GPs)
        for i in range(self.num_GPs):
            output_data_compressed = output_data_compressed.at[i].set(self.GPs[i].predict(parameters_normalized))    # this is the time consuming part

        # Untransform the output data.
        output_data_normalized = self.data_processor.decompress_data(output_data_compressed)

        # Then we denormalize the output data.
        output_data = self.data_processor.denormalize_data(output_data_normalized)
        
        return output_data
    
    # @partial(jit, static_argnums=0) 
    def predict_value_and_std(self, parameters):
        # Predict the quantity for the given parameters.
        # First we normalize the parameters.
        parameters_normalized = self.data_processor.normalize_input_data(parameters)

        # Then we predict the output data for the normalized parameters.
        output_data_compressed = jnp.zeros(self.num_GPs)
        output_std_compressed = jnp.zeros(self.num_GPs)

        for i in range(self.num_GPs):
            v, s = self.GPs[i].predict_value_and_std(parameters_normalized)
            output_data_compressed = output_data_compressed.at[i].set(v)   
            output_std_compressed = output_std_compressed.at[i].set(s)   

        # Untransform the output data.
        output_data_normalized = self.data_processor.decompress_data(output_data_compressed)
        output_std_normalized = self.data_processor.decompress_std(output_std_compressed)

        # Then we denormalize the output data.
        output_data = self.data_processor.denormalize_data(output_data_normalized)
        output_std = self.data_processor.denormalize_std(output_std_normalized)
        
        return output_data, output_std
    
    # @partial(jit, static_argnums=0) 
    def sample_prediction(self, parameters, N=1, RNGkey=random.PRNGKey(time.time_ns())):
        # Predict the quantity for the given parameters.
        # First we normalize the parameters.
        parameters_normalized = self.data_processor.normalize_input_data(parameters)

        # Then we predict the output data for the normalized parameters.
        output_data_compressed = jnp.zeros((N, self.num_GPs))
        for i in range(self.num_GPs):
            _, RNGkey = self.GPs[i].sample(parameters_normalized, RNGkey=RNGkey)            
            output_data_compressed = output_data_compressed.at[:,[i]].set(_)    # this is the time consuming part

        # Untransform the output data.
        output_data_normalized = jnp.array([self.data_processor.decompress_data(output_data_compressed[i,:]) for i in range(N)])

        # Then we denormalize the output data.
        output_data = self.data_processor.denormalize_data(output_data_normalized)
        return output_data, RNGkey
    
    # @partial(jit, static_argnums=0) 
    def predict_gradients(self, parameters):
        # Predict the quantity for the given parameters.
        # First we normalize the parameters.
        parameters_normalized = self.data_processor.normalize_input_data(parameters)

        # Then we predict the output data for the normalized parameters.
        output_data_compressed = np.zeros((self.num_GPs, self.input_size))

        for i in range(self.num_GPs):
            output_data_compressed[i] = self.GPs[i].predict_gradient(parameters_normalized.copy())

        output_data_compressed = jnp.array(output_data_compressed)

        # data out
        data_out = np.zeros((self.output_size, self.input_size))

        # note that in order to get the gradients we have to scale it twice with input and output normalization
        
        for i in range(self.input_size):
            data_out[:,i] = self.data_processor.decompress_data(output_data_compressed[:,i]) 
            data_out[:,i] /= self.data_processor.input_stds[i]

        for j in range(self.output_size):
            data_out[j,:] *= self.data_processor.output_stds[j]
        
        return data_out.T
    
    def update_error(self,point):
        #print(point)
        #print(self.num_GPs)
        for i in range(self.num_GPs):

            mean,std = self.GPs[i].predict_value_and_std(point)
            if self.GPs[i].hyperparameters['error_tolerance'] > std**2 /3.: 
                # more than a third of uncertanity is error
                self.GPs[i].hyperparameters['error_tolerance'] /= 2.
                # this is still very simple. We should relate it to the impact on final loglike
                # in this case the error of all comps will evolve similiar

    def train(self):
        # Train the GP emulator.

        input_data = self.data_processor.input_data_normalized
        output_data = self.data_processor.output_data_emulator

        self.num_GPs = output_data.shape[1]

        # if there are not Gps yet we need to create them
        if self.num_GPs > len(self.GPs) :
            for i in range(len(self.GPs), output_data.shape[1]):
                # Create a GP for each dimension of the output_data.
                self.GPs.append(copy.deepcopy(GP('GP '+self.quantity_name+' dim '+str(i), **self.hyperparameters)))


        for i in range(self.num_GPs):

            # Load the data into the GP.
            self.GPs[i].load_data(input_data, output_data[:,i])


            # Train the GP.
            self.GPs[i].train()

        # if we have a plotting directory, we will run some tests
        if self.hyperparameters['plotting_directory'] is not None and self.hyperparameters['testset_fraction'] is not None:
            self.run_tests()

        pass

    def train_single_GP(self, input_data, output_data):
        # Train the GP emulator.
        D = gpx.Dataset(input_data, output_data)

        pass

    def load_data(self, input_data_raw, output_data_raw):
        # Load the raw data from the data cache.
        self.data_processor.clean_data()
        self.data_processor.load_data(input_data_raw, output_data_raw)
        pass

    def set_parameters(self, parameters):
        # Set the parameters of the emulator.
        self.data_processor.set_parameters(parameters)
        pass

    def run_tests(self):
        # Run tests for the emulator.
        np.random.seed(0)
        train_indices, test_indices = np.split(np.random.permutation(self.GPs[0].D.n), [int((1-self.hyperparameters['testset_fraction'])*self.GPs[0].D.n)])

        # predict the test set
        for i in range(len(test_indices)):
            self.debug('Predicting test set point: ', i)
            prediction, std = self.predict_value_and_std(self.data_processor.input_data_raw[jnp.array([test_indices[i]])])

            true = self.data_processor.output_data_raw[jnp.array([test_indices[i]])]

            # check that plotting_dir/preictions/ exists
            if not os.path.exists(self.hyperparameters['plotting_directory']+ "/predictions/"):
                os.makedirs(self.hyperparameters['plotting_directory']+ "/predictions/")

            # plot the prediction
            plot_prediction_test(prediction, true, std, self.quantity_name, self.data_processor.input_data_raw[jnp.array(test_indices[i])], self.hyperparameters['plotting_directory']+ "/predictions/"+self.quantity_name+'_prediction_'+str(i)+'.png')



        pass


class GP(BaseClass):

    def __init__(self, name=None, **kwargs):
        super().__init__(name, **kwargs)

        # default hyperparameters
        defaulthyperparameters = {
            # Kernel type
            'kernel': 'RBF',
            # Exponential decay learning rate
            'learning_rate': 0.02,
            # Number of iterations
            'num_iters': 100,

            # plotting directory
            'plotting_directory': None,

            # testset fraction. If we have a testset, which is not None, then we will use this fraction of the data as a testset
            'testset_fraction': None,

            #error
            'error_tolerance': 0.1,

            # numer of test samples to determine the quality of the emulator
            'N_quality_samples': 5,
            
        }

        # The hyperparameters are a dictionary of the hyperparameters for the different quantities. The keys are the names of the quantities.
        self.hyperparameters = defaulthyperparameters

        # Flag that indicates whether it is required to recompute the inverse kernel matrix (inv_Kxx) of the training data set.
        self.recompute_kernel_matrix = False
        self.inv_Kxx = None

        self.D = None
        self.opt_posterior = None

        for key, value in kwargs.items():
            self.hyperparameters[key] = value

        pass

    # def __del__(self):
    #     # remove the GP
    #     del self.D
    #     del self.opt_posterior
    #     self.opt_posterior = None
    #     self.D = None
    #     self.input_data = None
    #     self.output_data = None

    #     gc.collect()


    def load_data(self, input_data, output_data):
        # Load the data from the data processor.
        self.recompute_kernel_matrix = True
        self.input_data = input_data
        self.output_data = output_data[:,None]
        del self.D
        gc.collect()
        self.D = gpx.Dataset(self.input_data, self.output_data)
        self.test_D = None


        # if we have a test fraction, then we will split the data into a training and a test set
        if self.hyperparameters['testset_fraction'] is not None:
            self.debug('Splitting data into training and test set')
            np.random.seed(0)
            train_indices, test_indices = np.split(np.random.permutation(self.D.n), [int((1-self.hyperparameters['testset_fraction'])*self.D.n)])
            self.D = gpx.Dataset(self.input_data[train_indices], self.output_data[train_indices])
            self.test_D = gpx.Dataset(self.input_data[test_indices], self.output_data[test_indices])

        pass

    def train(self):
        # Train the GP emulator.
        
        # Create the kernel

        if self.hyperparameters['kernel'] == 'RBF':

            kernelRBF = gpx.kernels.RBF() #+ gpx.kernels.White()
        
            #kernelM52 = gpx.kernels.Matern52()
            #kernelLin = gpx.kernels.Polynomial(degree=1)
            kernelWhite = gpx.kernels.White(variance = self.hyperparameters['error_tolerance'] )
            kernelError = kernelWhite.replace_trainable(variance=False)
            kernel = gpx.kernels.SumKernel(kernels = [kernelRBF,kernelError])

        else:
            raise ValueError('Kernel not implemented')
        
        meanf = gpx.mean_functions.Zero()
        prior = gpx.gps.Prior(mean_function=meanf, kernel=kernel)

        # Create the likelihood
        likelihood = gpx.gps.Gaussian(num_datapoints=self.D.n)
        

        posterior = prior * likelihood

        negative_mll = gpx.objectives.ConjugateMLL(negative=True)
        negative_mll(posterior, train_data=self.D)

        #negative_mll = jit(negative_mll)

        # have exponential decay learning rate
        #lr = lambda t: jnp.exp(-self.hyperparameters['learning_rate']*t)
        lr = lambda t: self.hyperparameters['learning_rate']*10.

        del self.opt_posterior
        self.opt_posterior = None
        gc.collect()

        # fit
        self.opt_posterior, history = gpx.fit(
            model=posterior,
            objective=negative_mll,
            train_data=self.D,
            optim=ox.adam(learning_rate=lr),
            num_iters=self.hyperparameters['num_iters'],
            safe=True, # what does this do?
            key=jax.random.PRNGKey(0),
        )

        # negative_mll._clear_cache()
        # del negative_mll
        # negative_mll = None

        gc.collect()

        # some debugging output
        if self.hyperparameters['plotting_directory'] is not None:
            # creat directory if not exist
            import os
            if not os.path.exists(self.hyperparameters['plotting_directory']+ "/loss/"):
                os.makedirs(self.hyperparameters['plotting_directory']+ "/loss/")
            loss_plot(history, self._name , self.hyperparameters['plotting_directory']+'/loss/' + self._name + '_loss.png')
            
            # plot the training data
            #fig = plt.figure()
            #cmap = mpl.cm.cool

            #norm = mpl.colors.Normalize(vmin=-3, vmax=3)
            #ax.scatter(self.input_data[:,0],self.input_data[:,1], self.input_data[:,2],c=self.output_data[:,0], cmap=cmap)
            #plt.savefig(self.hyperparameters['plotting_directory']+'/loss/' + self._name + '_trainData.png')
            #plt.close(fig)
            

            # plot a slice of the trained GP
            #fig = plt.figure()
            #ys = jnp.linspace(0., 3., 100)
            #zs = []
            #stds = []
            #for yval in ys:
            #    inputDat = jnp.array([[yval,yval,yval]])
            #    mean,std = self.predict_value_and_std(inputDat)
            #    zs.append(mean)
            #    stds.append(std)
            #zs = np.array(zs)
            #stds = np.array(stds)
            #plt.plot(ys,zs)
            #plt.fill_between(ys, zs - 3.*stds,zs + 3.*stds,alpha = 0.3)
            #plt.fill_between(ys, zs - stds,zs + stds,alpha = 0.3)
            #plt.savefig(self.hyperparameters['plotting_directory']+'/loss/' + self._name + '_slice.png')
            #plt.close(fig)
            

            
            if self.hyperparameters['testset_fraction'] is not None:
                self.run_test_set_tests()

        pass

    # @partial(jit, static_argnums=0) 
    def predict(self, input_data):
        # Predict the output data for the given input data.


        # OLD CODE
        # latent_dist = self.opt_posterior.predict(input_data, train_data=self.D)
        # predictive_dist = self.opt_posterior.likelihood(latent_dist)
        # predictive_mean = predictive_dist.mean()
        # ab = self.opt_posterior.predict_mean_single(input_data, self.D)

        if self.recompute_kernel_matrix:
            inv_Kxx = self.opt_posterior.compute_inv_Kxx(self.D)
            #self.recompute_kernel_matrix = False    # TODO: This leads to memory leaks in jit mode
            self.inv_Kxx = inv_Kxx
        else:
            inv_Kxx = self.inv_Kxx

        ac = self.opt_posterior.calculate_mean_single_from_inv_Kxx(input_data, self.D, inv_Kxx)

        return ac
        
    # @partial(jit, static_argnums=0) 
    def predict_value_and_std(self, input_data, return_std=False):
        # Predict the output data for the given input data.
        inv_Kxx = self.opt_posterior.compute_inv_Kxx(self.D)

        ac = self.opt_posterior.calculate_mean_single_from_inv_Kxx(input_data, self.D, inv_Kxx)

        latent_dist = self.opt_posterior.predict(input_data, train_data=self.D)
        predictive_dist = self.opt_posterior.likelihood(latent_dist)
        predictive_std = predictive_dist.stddev()
        return ac, predictive_std[0]


    # @partial(jit, static_argnums=0) 
    def sample(self, input_data, RNGkey=random.PRNGKey(time.time_ns())):
        # Predict the output data for the given input data.
        N = self.hyperparameters['N_quality_samples']

        if self.recompute_kernel_matrix:
            inv_Kxx = self.opt_posterior.compute_inv_Kxx(self.D)
            self.inv_Kxx = inv_Kxx
        else:
            inv_Kxx = self.inv_Kxx

        ac = self.opt_posterior.calculate_mean_single_from_inv_Kxx(input_data, self.D, inv_Kxx)

        # DOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOo
        latent_dist = self.opt_posterior.predict(input_data, train_data=self.D)
        predictive_dist = self.opt_posterior.likelihood(latent_dist)
        predictive_std = predictive_dist.stddev()
        # predictive_mean = predictive_dist.mean()
        
        # generate new key
        RNGkey, subkey = random.split(RNGkey)

        return random.normal(key= subkey, shape=(N,1)) * jnp.sqrt(predictive_std[0]) + ac, RNGkey
        
    # def predict_gradient(self, input_data):
    #     # Predict the gradient of the output data for the given input data.
    #     gradient = grad(self.opt_posterior.predict_mean_single)

    #     return gradient(input_data, self.D)



    # Some debugging functions
    def run_test_set_tests(self):
        # This function is used to test the test set.
        means = jnp.zeros(self.test_D.n)
        stds = jnp.zeros(self.test_D.n)

        # predict the test set
        for i in range(self.test_D.n):
            mean, std = self.predict_value_and_std(jnp.array([self.test_D.X[i]]))
            means = means.at[i].set(mean)
            stds = stds.at[i].set(std)
            self.debug('Predicted: ', mean, ' True: ', self.test_D.y[i], ' Error: ', mean - self.test_D.y[i])

        # calculate the mean squared error
        mse = jnp.mean((mean - self.test_D.y)**2)

        # test that the directory exists
        if not os.path.exists(self.hyperparameters['plotting_directory']+ "/test_set_prediction/"):
            os.makedirs(self.hyperparameters['plotting_directory']+ "/test_set_prediction/")

        # plot the mean and the std
        plot_pca_components_test_set(jnp.array(self.test_D.y)[:,0], means, stds,self._name , self.hyperparameters['plotting_directory']+'/test_set_prediction/'+self._name+'.png')
        
        pass