import sys
import os
import torch
import pandas as pd
import datetime
from argparse import ArgumentParser
from torch import nn, optim
import torch.nn.functional as F
from torch.utils.data import DataLoader, random_split

import pytorch_lightning as pl
from pytorch_lightning.metrics import functional as FM

from network.ecgresnet_mcdropout_auxout import ECGResNet_MCDropout_AuxOutput
from utils.helpers import create_results_directory
from utils.focalloss_weights import FocalLoss

class ECGResNetMCDropout_AuxOutSystem(pl.LightningModule):
    """
    This class implements the ECGResNet with Monte Carlo dropout and Auxiliary output in PyTorch Lightning.
    It can estimate the epistemic and aleatoric uncertainty of its predictions.
    """

    def __init__(self, in_channels, n_grps, N, 
                 num_classes, dropout, first_width, stride, 
                 dilation, learning_rate, n_dropout_samples, sampling_dropout_rate, n_logit_samples, loss_weights=None, 
                 **kwargs):
        """
        Args:
          in_channels: number of channels of input
          n_grps: number of ResNet groups
          N: number of blocks per groups
          num_classes: number of classes of the classification problem
          dropout: probability of an argument to get zeroed in the dropout layer
          first_width: width of the first input
          stride: tuple with stride value per block per group
          dilation: spacing between the kernel points of the convolutional layers
          learning_rate: the learning rate of the model
          n_dropout_samples: number of Monte Carlo dropout samples to take
          sampling_dropout_rate: the ratio of dropped-out neurons during Monte Carlo sampling
          n_logit_samples: number of logit samples of the auxiliary output
          loss_weights: array of weights for the loss term
        """
        super().__init__()
        self.save_hyperparameters()
        self.learning_rate = learning_rate
        self.n_dropout_samples = n_dropout_samples
        self.n_logit_samples = n_logit_samples


        self.register_buffer('IDs', torch.empty(0).type(torch.LongTensor))
        self.register_buffer('predicted_labels', torch.empty(0).type(torch.LongTensor))
        self.register_buffer('correct_predictions', torch.empty(0).type(torch.BoolTensor))
        self.register_buffer('aleatoric_uncertainty', torch.empty(0).type(torch.FloatTensor))
        self.register_buffer('epistemic_uncertainty', torch.empty(0).type(torch.FloatTensor))
        self.register_buffer('total_uncertainty', torch.empty(0).type(torch.FloatTensor))

        self.model = ECGResNet_MCDropout_AuxOutput(in_channels, 
                               n_grps, N, num_classes, 
                               dropout, first_width, 
                               stride, dilation, n_dropout_samples, sampling_dropout_rate, n_logit_samples)


        if loss_weights is not None:
            weights = torch.tensor(loss_weights, dtype = torch.float)
        else:
            weights = loss_weights

        self.loss = FocalLoss(gamma=1, weights = weights)

    def forward(self, x):
        """
        Performs a forward through the model.

        Args:
            x (tensor): Input data.

        Returns:
            output1: output at the auxiliary point of the ECGResNet
            output2_mean: output at the end of the model
            output2_log_var: the predicted log variance of the model
        """
        output1, output2_mean, output2_log_var = self.model(x)
        return output1, output2_mean, output2_log_var

    def training_step(self, batch, batch_idx):
        """Performs a training step.

        Args:
            batch (dict): Output of the dataloader.
            batch_idx (int): Index no. of this batch.

        Returns:
            tensor: Total loss for this step.
        """
        data, target = batch['waveform'], batch['label']

        # Make prediction
        output1, output2_mean, output2_log_var = self(data)

        # Sample from logits, returning a vector x_i
        x_i = self.model.sample_logits(self.n_logit_samples, output2_mean, output2_log_var, average=True)
            
        train_loss1 = self.loss(output1, target)
        train_loss2 = self.loss(x_i, target)
        total_train_loss = (0.3 * train_loss1) + train_loss2
        
        self.log('train_loss', total_train_loss)

        return {'loss': total_train_loss}
    
    def validation_step(self, batch, batch_idx):
        data, target = batch['waveform'], batch['label']

        # Make prediction
        _, output2_mean, output2_log_var = self(data)
            
        # Sample from logits, returning a  vector x_i
        x_i = self.model.sample_logits(self.n_logit_samples, output2_mean, output2_log_var, average=True)
        
        # Apply softmax to obtain probability vector p_i
        p_i = F.softmax(x_i, dim=1)
        
        val_loss = self.loss(x_i, target)
        acc = FM.accuracy(p_i, target)

        # Log metrics
        metrics = {'val_loss': val_loss.item(), 'val_acc': acc.item()}
        self.log('val_acc', acc.item())
        self.log('val_loss', val_loss.item())
        return metrics

    def on_test_epoch_start(self):
        """
        Enable dropout at test time.
        """
        self.model.enable_dropout()

    def test_step(self, batch, batch_idx, save_to_csv=False):
        data, target = batch['waveform'], batch['label']

        # MC sample using dropout, sample logits for every mc-dropout sample
        predictions, predictions_mean, predictions_var, log_variances_mean, predictions_mean_no_sm  = self.model.mc_sample_with_sample_logits(data)

        # Take exponent to get the variance
        output2_var = log_variances_mean.exp()

        predicted_labels = predictions_mean.argmax(dim=1).type_as(data)
        correct_predictions = torch.eq(predicted_labels, target)

        # MC dropout variance over predicted labels (epistemic uncertainty)
        sampled_var = torch.gather(predictions_var, 1, predictions_mean.argmax(dim=1).unsqueeze_(1))[:, 0]

        # Predicted aux-out variance of the predicted label (aleatoric uncertainty)
        predicted_labels_predicted_var = torch.gather(output2_var, 1, predictions_mean.argmax(dim=1).unsqueeze_(1))[:, 0]

        # Total uncertainty
        total_var = predicted_labels_predicted_var + sampled_var
        
        # Get metrics
        test_loss = self.loss(predictions_mean.type_as(data), target)
        acc = FM.accuracy(predictions_mean, target)

        self.log('test_acc', acc.item())
        self.log('test_loss', test_loss.item())

        self.IDs = torch.cat((self.IDs, batch['id']), 0)
        self.predicted_labels = torch.cat((self.predicted_labels, predicted_labels), 0)
        self.correct_predictions = torch.cat((self.correct_predictions, correct_predictions), 0)
        self.aleatoric_uncertainty = torch.cat((self.aleatoric_uncertainty, predicted_labels_predicted_var), 0)
        self.epistemic_uncertainty = torch.cat((self.epistemic_uncertainty, sampled_var), 0)
        self.total_uncertainty = torch.cat((self.total_uncertainty, total_var), 0)

        return {'test_loss': test_loss.item(), 'test_acc': acc.item()}

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=self.learning_rate)

        return optimizer

    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument('--model_name', type=str, default='none_auxout')
        parser.add_argument('--n_logit_samples', type=int, default=100) # Number of logit samples of the auxiliary output
        parser.add_argument('--n_dropout_samples', type=int, default=20)  # Number of dropout samples during MCDropout sampling
        parser.add_argument('--sampling_dropout_rate', type=float, default=0.1) # Dropout rate during MCDropout sampling
        parser.add_argument('--ensembling_method', type=bool, default=False)
        return parser

    # Combine results into single dataframe and save to disk
    def save_results(self):
        """
        Combine results into single dataframe and save to disk as .csv file
        """
        results = pd.concat([
            pd.DataFrame(self.IDs.cpu().numpy(), columns= ['ID']),  
            pd.DataFrame(self.predicted_labels.cpu().numpy(), columns= ['predicted_label']),
            pd.DataFrame(self.correct_predictions.cpu().numpy(), columns= ['correct_prediction']),
            pd.DataFrame(self.aleatoric_uncertainty.cpu().numpy(), columns= ['aleatoric_uncertainty']), 
            pd.DataFrame(self.epistemic_uncertainty.cpu().numpy(), columns= ['epistemic_uncertainty']), 
            pd.DataFrame(self.total_uncertainty.cpu().numpy(), columns= ['total_uncertainty']), 
        ], axis=1)

        create_results_directory()
        results.to_csv('results/{}_{}_results.csv'.format(self.__class__.__name__, datetime.datetime.now().replace(microsecond=0).isoformat()), index=False)
