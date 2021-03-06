# motivated by http://lasagne.readthedocs.org/en/latest/user/tutorial.html
#created by nmew
import os
import sys
import time
import logging
from argparse import ArgumentParser

import numpy as np
import theano
import theano.tensor as T
import lasagne

module_path = os.path.realpath(os.path.dirname(__file__))
parent_path = os.path.join(module_path, "..")
sys.path.append(parent_path)
from common.util import config_file_logging, ready_dir, write_args_file
import common.nn_reporter as nnr
import common.pickled_data_loader as pdl


TRAIN_PERCENTAGE = .9
BATCH_SIZE = 100  # larger=faster epochs; smaller=better loss/epoch
NUM_EPOCHS = 500
LEARNING_RATE = 0.001
MOMENTUM = 0.9

HIDDEN_LAYERS = [
    # (num_units, dropout_p, nonlinearity)
    (512, .10, lasagne.nonlinearities.rectify),
    (256, .25, lasagne.nonlinearities.rectify),
    (128, .25, lasagne.nonlinearities.rectify),
] # whoakay; rectify >> sigmoid in my testing so far!
OUTPUT_ACTIVATION = lasagne.nonlinearities.rectify
USE_DROPOUT = True

# None means it'll seed itself from /de/urandom or equivalent.
# (Note np & lasagne must each be seeded explicitly in code below.)
RANDOM_STATE = 42 #None

# shuffle the data, and return a portion for test/train
def get_train_test_indices(num_compounds, train_percentage=TRAIN_PERCENTAGE, random_state=RANDOM_STATE):
    rng = np.random.RandomState(seed=random_state)
    shuffled_indices = np.arange(num_compounds) #array of length(num_compounds). Defines the array
    rng.shuffle(shuffled_indices)
    train_indices = shuffled_indices[:int(num_compounds * train_percentage)]
    # to create a small train/test set. below returns a test set equal in size to the train set. Note that if
    # TRAIN_PERCENTAGE > 0.5, this will return redundant data
    #test_indices = shuffled_indices[(num_compounds - num_compounds * train_percentage):]
    test_indices = shuffled_indices[int(num_compounds * train_percentage):]
    return train_indices, test_indices


def load_dataset(training_data_filenames, test_indices_file=None):
    compound_ids, target_ids, \
    fingerprints, compound_target_affinity = pdl.load_data(*training_data_filenames)
    if test_indices_file is not None:
        test_indices = pdl.load_indices(test_indices_file)
        train_indices = np.delete(np.arange(fingerprints.shape[0]), test_indices)
    else:
        train_indices, test_indices = get_train_test_indices(fingerprints.shape[0])
    X_train = fingerprints[train_indices]
    y_train = compound_target_affinity[train_indices]
    X_val = fingerprints[test_indices]
    Y_val = compound_target_affinity[test_indices]
    return X_train, y_train, X_val, Y_val


def iterate_minibatches(inputs, targets, batchsize, shuffle=False):
    assert len(inputs) == len(targets)
    if shuffle:
        indices = np.arange(len(inputs))
        np.random.shuffle(indices)
    for start_idx in range(0, len(inputs) - batchsize + 1, batchsize):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batchsize]
        else:
            excerpt = slice(start_idx, start_idx + batchsize)
        yield inputs[excerpt], targets[excerpt]


def build_nn(input_shape, output_shape, input_var, use_dropout=USE_DROPOUT, hidden_layers=HIDDEN_LAYERS,
             output_activation=OUTPUT_ACTIVATION, random_state=RANDOM_STATE):
    # for determinism (https://groups.google.com/forum/#!topic/lasagne-users/85-6gxygtIo)
    lasagne.layers.noise._srng = lasagne.layers.noise.RandomStreams(random_state)
    
    # define input layer
    nnet = lasagne.layers.InputLayer(shape=(None, input_shape),
                                     input_var=input_var)
    # define hidden layer(s)
    for nnodes, dropout, nonlin in hidden_layers:
        nnet = lasagne.layers.DenseLayer(
            nnet, num_units=nnodes,
            nonlinearity=nonlin,
            W=lasagne.init.GlorotUniform())
        if use_dropout:
            nnet = lasagne.layers.DropoutLayer(
                nnet, p=dropout)
    
    # define Affinity output layer
    nnetA = lasagne.layers.DenseLayer(
        nnet, num_units=output_shape,
        nonlinearity=output_activation)
    # define Error output layer
    nnetE = lasagne.layers.DenseLayer(
        nnet, num_units=output_shape,
        nonlinearity=output_activation)
    # return network
    return nnetA, nnetE
    


def train(training_data_filenames, output_dir, test_indices_filename=None, num_epochs=NUM_EPOCHS,
          learning_rate=LEARNING_RATE, momentum=MOMENTUM, batch_size=BATCH_SIZE, build_nn_func=build_nn):
    """
    Build and train the neural network
    :param momentum:
    :param training_data_filenames: tuple with (fingerprint filename, target_association filename)
    :param output_dir: directory where any output files will be written
    :param test_indices_filename: path to file with pickled array of indices to be used for a test holdout set
    :param num_epochs: number of epochs to train on
    :param batch_size: size of mini-batches to train
    :param learning_rate: learning rate (float value)
    :param momentum: momentum (float value)
    :param build_nn_func: function that builds build_nn_func
    """
    # load data
    X_train, y_train, X_val, y_val = load_dataset(training_data_filenames, test_indices_file=test_indices_filename)
    
    # prepare theano variables
    input_var = T.fmatrix('inputs')    # fmatrix b/c it's a batch of float? vectors
    target_var = T.fmatrix('targets')  # fmatrix b/c it's a batch of float vectors
    error_var = T.fmatrix('errors')  # fmatrix b/c it's a batch of float vectors

    # build nn
    networkAff,networkErr = build_nn_func(X_train.shape[1], y_train.shape[1], input_var)
    
    # numpy array is a tuple (rnumber row, number columns) that defines the shape of matrix
    # columns is the number of total number of features/measurements in the fingerprint
    # rows number of molecules

    #y_train is a vector with 1 value for the affinity for each target a molecule bound to

    # generate predicitons
    aff_pred = lasagne.layers.get_output(networkAff)
    err_pred = lasagne.layers.get_output(networkErr)
    # get loss and update expressions
    lossAff1 = lasagne.objectives.squared_error(aff_pred, target_var)
    lossErr = lasagne.objectives.squared_error(err_pred, error_var)

    masked_Aff_targets = T.gt(target_var, 0) #gt for GreaterThan >
    masked_Err_targets = T.gt(error_var, 0) 

    lossAff2 = lasagne.objectives.aggregate(lossAff1, weights=masked_targets, mode='normalized_sum')
    lossErr = lasagne.objectives.aggregate(lossErr, weights=T.gt(error_var, 0), mode='normalized_sum') 
    # T.gt() is t.GreaterThan. returns array of the same shape as target var of booleans. Takes loss function and multiplies it by the weights. 0's if
    # there is no affinity to predict. 

    paramsAff = lasagne.layers.get_all_params(networkAff, trainable=True)
    paramsErr = lasagne.layers.get_all_params(networkErr, trainable=True)

    updatesAff = lasagne.updates.nesterov_momentum(lossAff2, paramsAff, learning_rate=learning_rate, momentum=momentum)
    updatesErr = lasagne.updates.nesterov_momentum(lossErr, paramsErr, learning_rate=learning_rate, momentum=momentum)

    updated_Aff_weights = lossAff1 * masked_Aff_targets
    updated_Err_weights = lossErr * masked_Err_targets

    # compile training function
    train_fnAff = theano.function([input_var, target_var], (updated_weights,lossAff2), updates=updatesAff)
    # returns tuple with (matrixOfErrors,meanError)
    train_fnErr = theano.function([input_var, error_var], lossErr, updates=updatesErr)

    # get predictions and loss for aff
    test_prediction_aff = lasagne.layers.get_output(networkAff, deterministic=True)
    test_loss_aff = lasagne.objectives.squared_error(test_prediction_aff, target_var)
    test_loss_mean_aff = lasagne.objectives.aggregate(test_loss_aff, T.gt(target_var, 0), mode='mean')
    
    # get predictions and loss for err
    test_prediction_err = lasagne.layers.get_output(networkErr, deterministic=True)
    #get the squared error between the error prediction and magnitude of the true error
    test_loss_err = lasagne.objectives.squared_error(test_prediction_err, abs(test_prediction_aff - target_var))
    test_loss_mean_err = lasagne.objectives.aggregate(test_loss_err, T.gt(error_var, 0), mode='mean')

    ## note: test_acc is ommitted b/c it was with respect to softmax classification

    # compile test/validation function
    val_fn = theano.function([input_var, target_var], (test_loss,test_loss_mean))
    val_err_fn = theano.function([input_var, error_var], lossErr)
    val_errs = []
    train_errs = []

# in the mini_batch I'll add an additional training set. The output from the first training step (the error of the prediction of the affinity) 
# will be the input of the second training set, updating the error prediction. Could define a variable: trueError = y- yHat

    # loop through epochs
    for epoch in range(num_epochs):
        # In each epoch, we do a full pass over the training data:
        train_err = 0
        err_err = 0
        train_batches = 0
        start_time = time.time()
        #train affinity
        for inputs, targets in iterate_minibatches(X_train, y_train, batch_size, shuffle=True):
            # train one epoch
            # Note: there's no need for abs() because ReLU ensures that all predictions are positive, and our loss function for
            # both affinity and error is mean-squared error. So there will never be negative numbers.
            train_err_matrix, train_err_mean = train_fnAff(inputs,targets)
            err_err += train_fnErr(inputs, train_err_matrix)
            train_err += train_err_mean
            train_batches += 1
        
        train_err = train_err / train_batches
        err_err = err_err / train_batches
        #_____________________________________
        # And a full pass over the validation data:
        val_err = 0
        val_err_err = 0 
        val_batches = 0
        for inputs, targets in iterate_minibatches(X_val, y_val, batch_size, shuffle=False):
            err_matrix, err = val_fn(inputs, targets)
            val_err_err = val_err_fn(inputs,err_matrix) #inputs are the fingerPrints, the inputs for the training
            val_err += err
            val_err_err += err_err
            val_batches += 1
        val_err = val_err / val_batches
        # Then we print the results for this epoch:
        logging.info("Epoch {} of {} took {:.3f}s".format(epoch + 1, num_epochs, time.time() - start_time))
        logging.info("  training loss:\t\t{:.9f}".format(train_err))
        logging.info("  validation loss:\t\t{:.9f}".format(val_err))
        logging.info("  error_error:\t\t{:.9f}".format(err_err))
        train_errs.append(train_err)
        val_errs.append(val_err)

        # Optionally, you could now dump the network weights to a file like this:
        if epoch % 10 == 0 or epoch == num_epochs - 1:
            np.savez(output_dir + '/aff_model_at_epoch_{}.npz'.format(epoch),
                     *lasagne.layers.get_all_param_values(networkAff))
            np.savez(output_dir + '/err_model_at_epoch_{}.npz'.format(epoch),
                     *lasagne.layers.get_all_param_values(networkErr)) 

    # GET STATISTICS
    #first recombining test/train matrices into fp and affinity
    #X_train, y_train, X_val, Y_val
    fingerprints = np.vstack(X_train,X_val)
    compound_target_affinity = np.vstack(y_train,Y_val)
    #get the indices for np.nonzeros
    nonzeros = np.nonzeros(compound_target_affinity)
    #compound_ids, target_ids

    #generate predictions for each network
    predictionAff = rnn.run_nn(fingerprints, network=networkAff)
    predictionErr = rnn.run_nn(fingerprints, network=networkErr)


    #calculate and plot r2 for complete chembl20
    nnr.compute_rsquared(predictionAff[nonzeros], compound_target_affinity[nonzeros],
                                  output_dir=output_path, result_name='r2Aff')
    nnr.plot_rsquared(predictionAff[nonzeros], compound_target_affinity[nonzeros],
                                  img_filename=output_dir + '/r2Affplot.png')

    nnr.compute_rsquared(predictionErr[nonzeros], compound_target_affinity[nonzeros],
                                  output_dir=output_path, result_name='r2err')
    nnr.plot_rsquared(predictionErr[nonzeros], compound_target_affinity[nonzeros],
                                  img_filename=output_dir + '/r2Errplot.png')

    np.savetxt(os.path.join(output_dir, 'train_errors.csv'), np.asarray(train_errs), delimiter=',')
    np.savetxt(os.path.join(output_dir, 'test_errors.csv'), np.asarray(val_errs), delimiter=',')
    nnr.plot_error(train_errs, title="Train Errors", filename=os.path.join(output_dir, 'train_errors.png'))
    nnr.plot_error(val_errs, title="Test Errors", filename=os.path.join(output_dir, 'test_errors.png'))

# get r^2 just for testing data:
    
    #get the indices for np.nonzeros
    nonzeros = np.nonzeros(y_val)
    #compound_ids, target_ids

    #generate predictions for each network
    predictionAff = rnn.run_nn(X_val, network=networkAff)
    predictionErr = rnn.run_nn(y_val, network=networkErr)
    compound_target_affinity = y_val

    #calculate and plot r2 for complete chembl20
    nnr.compute_rsquared(predictionAff[nonzeros], compound_target_affinity[nonzeros],
                                  output_dir=output_path, result_name='r2AffVal')
    nnr.plot_rsquared(predictionAff[nonzeros], compound_target_affinity[nonzeros],
                                  img_filename=output_dir + '/r2AffValplot.png')

    nnr.compute_rsquared(predictionErr[nonzeros], compound_target_affinity[nonzeros],
                                  output_dir=output_path, result_name='r2errVal')
    nnr.plot_rsquared(predictionErr[nonzeros], compound_target_affinity[nonzeros],
                                  img_filename=output_dir + '/r2ErrValplot.png')

    


#generate predictions for validation set:
# optimization: change predictionAff etc to using this format instead of calling rnn.run_nn as doing above on lines 244
#predictionAff_fn = theano.function([input_var], predictionAff)
#predictionErr_fn = theano.function([input_var], predictionErr)


def main(argv=sys.argv[1:]):
    parser = ArgumentParser("Train lasagne NN to find targets for given fingerprints", fromfile_prefix_chars='@')
    parser.add_argument('-o', '--output_directory', required=True,
                        help="directory where logging, backup and output will get stored")
    parser.add_argument('-f', '--fingerprints_dataset', type=str,
                        help="file with compound ids and fingerprints (*.csv)")
    parser.add_argument('-t', '--target_dataset', type=str,
                        help="file with target id, target chembl id, affinity and list of binding compounds (*.csv)")
    parser.add_argument('-e', '--num_epochs', type=int,
                        default=NUM_EPOCHS,
                        nargs='?',
                        help="number of epochs to train")
    parser.add_argument('-b', '--batch_size', type=int,
                        default=BATCH_SIZE,
                        nargs='?',
                        help="size of batches to train on")
    parser.add_argument('-l', '--learning_rate', type=int,
                        default=LEARNING_RATE,
                        nargs='?',
                        help="learning rate")
    parser.add_argument('-m', '--momentum', type=int,
                        default=MOMENTUM,
                        nargs='?',
                        help="momentum")
    parser.add_argument('-i', '--test-index-file', type=str,
                        nargs='?',
                        help='Pickled file of indices to use for testing')
    parser.add_argument('--log-level', type=str,
                        default='INFO',
                        nargs='?',
                        help='Output log level [default: %(default)s]')
    params, _ = parser.parse_known_args(argv)
    ready_dir(params.output_directory)
    logging.basicConfig(stream=sys.stderr, level=params.log_level)
    config_file_logging(params.output_directory)
    write_args_file(parser, params.output_directory)
    logging.info('Using random seed: {}'.format(RANDOM_STATE))

    train((params.fingerprints_dataset, params.target_dataset), params.output_directory, num_epochs=params.num_epochs,
          batch_size=params.batch_size, learning_rate=params.learning_rate, momentum=params.momentum)


if __name__ == '__main__':
    sys.exit(main())