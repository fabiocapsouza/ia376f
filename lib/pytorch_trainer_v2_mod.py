# -*- coding: utf-8 -*-
import os
import copy
import time
import pickle
import math
from collections import OrderedDict

import numpy as np
import torch
from torch.utils.data import TensorDataset
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torch.optim.lr_scheduler import _LRScheduler

try:
    import matplotlib.pyplot as plt
    from IPython import display
except:
    pass


class DeepNetTrainer(object):

    def __init__(self, model=None, criterion=None, optimizer=None, lr_scheduler=None, callbacks=None, devname='cpu'):

        self.dev_name = devname
        device = torch.device(self.dev_name)

        assert (model is not None)
        self.model = model.to(device)

        self.criterion = criterion
        self.optimizer = optimizer
        self.scheduler = lr_scheduler
        self.metrics = dict(train=OrderedDict(losses=[]), valid=OrderedDict(losses=[]))
        self.last_epoch = 0

        self.callbacks = []
        if callbacks is not None:
            for cb in callbacks:
                self.callbacks.append(cb)
                cb.trainer = self

    def fit(self, n_epochs, Xin, Yin, valid_data=None, valid_split=None, batch_size=10, shuffle=True):
        if valid_data is not None:
            train_loader = DataLoader(TensorDataset(Xin, Yin), batch_size=batch_size, shuffle=shuffle)
            valid_loader = DataLoader(TensorDataset(*valid_data), batch_size=batch_size, shuffle=shuffle)
        elif valid_split is not None:
            iv = int(valid_split * Xin.shape[0])
            Xval, Yval = Xin[:iv], Yin[:iv]
            Xtra, Ytra = Xin[iv:], Yin[iv:]
            train_loader = DataLoader(TensorDataset(Xtra, Ytra), batch_size=batch_size, shuffle=shuffle)
            valid_loader = DataLoader(TensorDataset(Xval, Yval), batch_size=batch_size, shuffle=shuffle)
        else:
            train_loader = DataLoader(TensorDataset(Xin, Yin), batch_size=batch_size, shuffle=shuffle)
            valid_loader = None
        self.fit_loader(n_epochs, train_loader, valid_data=valid_loader)

    def evaluate(self, Xin, Yin, metrics=None, batch_size=10):
        dloader = DataLoader(TensorDataset(Xin, Yin), batch_size=batch_size, shuffle=False)
        return self.evaluate_loader(dloader, metrics)

    def _do_optimize(self, X, Y):
        self.optimizer.zero_grad()
        Ypred = self.model.forward(X)
        loss = self.criterion(Ypred, Y)
        loss.backward()
        self.optimizer.step()
        return Ypred, loss

    def _do_evaluate(self, X, Y):
        Ypred = self.model.forward(X)
        loss = self.criterion(Ypred, Y)
        return Ypred, loss
    
    def _to_device(self, T, device):
        if isinstance(T, (list, tuple)):
            T = [self._to_device(t, device) for t in T]
        elif isinstance(T, dict):
            T = {k: self._to_device(t, device) for k, t in T.items()}
        else:
            T = T.to(device)
        return T

    def fit_loader(self, n_epochs, train_data, valid_data=None):
        device = torch.device(self.dev_name)
        self.has_validation = valid_data is not None
        self.n_batches = int(np.ceil(len(train_data.dataset)/train_data.batch_size)) # modificado Fabio 25set18
        try:
            for cb in self.callbacks:
                cb.on_train_begin(n_epochs, self.metrics)

            # for each epoch
            for curr_epoch in range(self.last_epoch + 1, self.last_epoch + n_epochs + 1):

                # training phase
                # ==============
                self.model = self.model.train()
                for cb in self.callbacks:
                    cb.on_epoch_begin(curr_epoch, self.metrics)

                epo_samples = 0
                epo_batches = 0
                epo_loss = 0

                # for each minibatch
                for curr_batch, (X, Y) in enumerate(train_data):

                    X = self._to_device(X, device)
                    Y = self._to_device(Y, device)

                    mb_size = X.size(0)
                    epo_samples += mb_size
                    epo_batches += 1

                    for cb in self.callbacks:
                        cb.on_batch_begin(curr_epoch, curr_batch, mb_size)

                    Ypred, loss = self._do_optimize(X, Y)

                    vloss = loss.data.cpu().item()
                    if hasattr(self.criterion, 'size_average') and self.criterion.size_average:
                        epo_loss += mb_size * vloss
                    else:
                        epo_loss += vloss

                    for cb in self.callbacks:
                        cb.on_batch_end(curr_epoch, curr_batch, X, Y, Ypred, loss)

                # end of training minibatches
                self.train_loss = float(epo_loss / epo_samples)
                self.metrics['train']['losses'].append(self.train_loss)

                # validation phase
                # ================
                if self.has_validation:
                    self.model = self.model.eval()
                    with torch.no_grad():
                        epo_samples = 0
                        epo_batches = 0
                        epo_loss = 0

                        # for each minibatch
                        for curr_batch, (X, Y) in enumerate(valid_data):

                            X = self._to_device(X, device)
                            Y = self._to_device(Y, device)

                            mb_size = X.size(0)
                            epo_samples += mb_size
                            epo_batches += 1

                            for cb in self.callbacks:
                                cb.on_vbatch_begin(curr_epoch, curr_batch, mb_size)

                            Ypred, loss = self._do_evaluate(X, Y)

                            if loss is None:
                                epo_loss = None
                            else:
                                vloss = loss.data.cpu().item()
                                if hasattr(self.criterion, 'size_average') and self.criterion.size_average:
                                    epo_loss += vloss * mb_size
                                else:
                                    epo_loss += vloss

                            for cb in self.callbacks:
                                cb.on_vbatch_end(curr_epoch, curr_batch, X, Y, Ypred, loss)

                        # end minibatches
                        if epo_loss is None:
                            self.valid_loss = None
                        else:
                            self.valid_loss = float(epo_loss / epo_samples)
                        self.metrics['valid']['losses'].append(self.valid_loss)

                else:
                    self.metrics['valid']['losses'].append(None)

                for cb in self.callbacks:
                    cb.on_epoch_end(curr_epoch, self.metrics)

                if self.scheduler is not None:
                    if self.scheduler.__class__.__name__ == 'ReduceLROnPlateau' and self.valid_loss is not None:
                        self.scheduler.step(self.valid_loss)
                    else:
                        self.scheduler.step()

        except KeyboardInterrupt:
            pass

        for cb in self.callbacks:
            cb.on_train_end(n_epochs, self.metrics)

    def evaluate_loader(self, data_loader, metrics=None, verbose=1):
        device = torch.device(self.dev_name)
        metrics = metrics or []
        my_metrics = dict(train=dict(losses=[]), valid=dict(losses=[]))
        for cb in metrics:
            cb.on_train_begin(1, my_metrics)
            cb.on_epoch_begin(1, my_metrics)

        epo_samples = 0
        epo_batches = 0
        epo_loss = 0

        try:
            with torch.no_grad():
                ii_n = len(data_loader)

                for curr_batch, (X, Y) in enumerate(data_loader):

                    X = self._to_device(X, device)
                    Y = self._to_device(Y, device)

                    mb_size = X.size(0)
                    epo_samples += mb_size
                    epo_batches += 1

                    Ypred, loss = self._do_evaluate(X, Y)

                    if loss is None:
                        epo_loss = None
                    else:
                        vloss = loss.data.cpu().item()
                        if hasattr(self.criterion, 'size_average') and self.criterion.size_average:
                            epo_loss += vloss * mb_size
                        else:
                            epo_loss += vloss

                    for cb in metrics:
                        cb.on_vbatch_end(1, curr_batch, X, Y, Ypred, loss)    # RAL, RCM

                    if verbose > 0:
                        print('\revaluate: {}/{}'.format(curr_batch, ii_n - 1), end='')

                if verbose > 0:
                    print(' ok')

        except KeyboardInterrupt:
            print(' interrupted!')

        if epo_loss is not None and epo_batches > 0:
            epo_loss /= epo_samples
            my_metrics['valid']['losses'].append(epo_loss)

        for cb in metrics:
            cb.on_epoch_end(1, my_metrics)

        # return dict([(k, v) for k, v in my_metrics['valid'].items()])
        return my_metrics['valid']

    def load_state(self, file_basename):
        device = torch.device(self.dev_name)
        load_trainer_state(file_basename, self.model, self.metrics)
        self.last_epoch = len(self.metrics['train']['losses'])
        self.model = self.model.to(device)

    def save_state(self, file_basename):
        cpu_model = self.model.to(torch.device('cpu'))
        save_trainer_state(file_basename, cpu_model, self.metrics)
        self.model = self.model.to(torch.device(self.dev_name))

    def predict_loader(self, data_loader):
        device = torch.device(self.dev_name)
        predictions = []
        with torch.no_grad():
            for X, _ in data_loader:
                X = X.to(device)
                Ypred = self.model(X)
                Ypred = Ypred.cpu().data
                predictions.append(Ypred)
        return torch.cat(predictions, 0)

    def predict(self, Xin):
        device = torch.device(self.dev_name)
        Xin = Xin.to(device)
        return predict(self.model, Xin)

    def predict_classes_loader(self, data_loader):
        y_pred = self.predict_loader(data_loader)
        _, pred = torch.max(y_pred, 1)
        return pred

    def predict_classes(self, Xin):
        device = torch.device(self.dev_name)
        Xin = Xin.to(device)
        return predict_classes(self.model, Xin)

    def predict_probas_loader(self, data_loader):
        y_pred = self.predict_loader(data_loader)
        probas = F.softmax(y_pred, dim=1)
        return probas

    def predict_probas(self, Xin):
        device = torch.device(self.dev_name)
        Xin = Xin.to(device)
        return predict_probas(self.model, Xin)

    def summary(self):
        pass
    
    
    def lr_find(self, dataloader, min_lr=1e-7, max_lr=10, linear=False, num_it=None):
        linear = linear
        num_it = num_it or len(dataloader)
        ratio = max_lr/min_lr
        lr_mult = (ratio/num_it) if linear else ratio**(1/num_it)
        best = 1e9
        
        optimizer = torch.optim.SGD(self.model.parameters(), lr=min_lr)
        if linear:
            lambda_schd = lambda it: lr_mult*it
        else:
            lambda_schd = lambda it: lr_mult**it
        lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda_schd)
        losses = []
        lrs = []
        self.save_state('tmp')
        
        device = torch.device(self.dev_name)
        self.model = self.model.train()
        
        try:
            for i, (X, Y) in enumerate(dataloader):
                print(i, end='\r')
                X = self._to_device(X, device)
                Y = self._to_device(Y, device)
                
                lr_scheduler.step()
                lr = optimizer.param_groups[0]['lr']
                lrs.append(lr)

                optimizer.zero_grad()
                Ypred = self.model.forward(X)
                loss = self.criterion(Ypred, Y)
                loss.backward()
                optimizer.step()

                iloss = loss.cpu().item()
                losses.append(iloss)
                if iloss < best:
                    best = iloss
                elif iloss > best * 10000 or i == num_it-1:
                    break
            
        except:
            raise
        finally:
            self.load_state('tmp')
            
        plt.ylabel("validation loss")
        plt.xlabel("learning rate (log scale)")
        plt.plot(lrs, losses)
        plt.xscale('log')
        
        return lrs, losses

        
def load_trainer_state(file_basename, model, metrics):
    model.load_state_dict(torch.load(file_basename + '.model', map_location=lambda storage, loc: storage))
    if os.path.isfile(file_basename + '.histo'):
        metrics.update(pickle.load(open(file_basename + '.histo', 'rb')))


def save_trainer_state(file_basename, model, metrics):
    torch.save(model.state_dict(), file_basename + '.model')
    pickle.dump(metrics, open(file_basename + '.histo', 'wb'))


def predict(model, Xin):
    y_pred = model.forward(Xin)
    return y_pred.data


def predict_classes(model, Xin):
    y_pred = predict(model, Xin)
    _, pred = torch.max(y_pred, 1)
    return pred


def predict_probas(model, Xin):
    y_pred = predict(model, Xin)
    probas = F.softmax(y_pred,dim=1)
    return probas


class Callback(object):
    def __init__(self):
        pass

    def on_train_begin(self, n_epochs, metrics):
        pass

    def on_train_end(self, n_epochs, metrics):
        pass

    def on_epoch_begin(self, epoch, metrics):
        pass

    def on_epoch_end(self, epoch, metrics):
        pass

    def on_batch_begin(self, epoch, batch, mb_size):
        pass

    def on_batch_end(self, epoch, batch, x, y, y_pred, loss):
        pass

    def on_vbatch_begin(self, epoch, batch, mb_size):
        pass

    def on_vbatch_end(self, epoch, batch, x, y, y_pred, loss):
        pass


class AccuracyMetric(Callback):
    def __init__(self):
        super().__init__()
        self.name = 'acc'

    def on_batch_end(self, epoch_num, batch_num, x, y_true, y_pred, loss):
        _, preds = torch.max(y_pred.data, 1)
        ok = (preds == y_true.data).sum()
        self.train_accum += ok.item()
        self.n_train_samples += y_pred.size(0)

    def on_vbatch_end(self, epoch_num, batch_num, x, y_true, y_pred, loss):
        _, preds = torch.max(y_pred.data, 1)
        ok = (preds == y_true.data).sum()
        self.valid_accum += ok.item()
        self.n_valid_samples += y_pred.size(0)

    def on_epoch_begin(self, epoch_num, metrics):
        self.train_accum = 0
        self.valid_accum = 0
        self.n_train_samples = 0
        self.n_valid_samples = 0

    def on_epoch_end(self, epoch_num, metrics):
        if self.n_train_samples > 0:
            metrics['train'][self.name].append(1.0 * self.train_accum / self.n_train_samples)
        if self.n_valid_samples > 0:
            metrics['valid'][self.name].append(1.0 * self.valid_accum / self.n_valid_samples)

    def on_train_begin(self, n_epochs, metrics):
        metrics['train'][self.name] = []
        metrics['valid'][self.name] = []


class ModelCheckpoint(Callback):

    def __init__(self, model_basename, reset=False, verbose=0, load_best=False):
        super().__init__()
        os.makedirs(os.path.dirname(model_basename), exist_ok=True)
        self.basename = model_basename
        self.reset = reset
        self.verbose = verbose
        self.load_best = load_best

    def on_train_begin(self, n_epochs, metrics):
        if (self.basename is not None) and (not self.reset) and (os.path.isfile(self.basename + '.model')):
            load_trainer_state(self.basename, self.trainer.model, self.trainer.metrics)
            if self.verbose > 0:
                print('Model loaded from', self.basename + '.model')

        self.trainer.last_epoch = len(self.trainer.metrics['train']['losses'])
        if self.trainer.scheduler is not None:
            self.trainer.scheduler.last_epoch = self.trainer.last_epoch

        self.best_model = copy.deepcopy(self.trainer.model)
        self.best_epoch = self.trainer.last_epoch
        self.best_loss = 1e10
        if self.trainer.last_epoch > 0:
            self.best_loss = self.trainer.metrics['valid']['losses'][-1] or self.trainer.metrics['train']['losses'][-1]

    def on_train_end(self, n_epochs, metrics):
        if self.verbose > 0:
            print('Best model was saved at epoch {} with loss {:.5f}: {}'
                  .format(self.best_epoch, self.best_loss, self.basename))
        if self.load_best:
            load_trainer_state(self.basename, self.trainer.model, self.trainer.metrics)
            if self.verbose > 0:
                print('Model loaded from', self.basename + '.model')

    def on_epoch_end(self, epoch, metrics):
        eloss = metrics['valid']['losses'][-1] or metrics['train']['losses'][-1]
        if eloss < self.best_loss:
            self.best_loss = eloss
            self.best_epoch = epoch
            self.best_model = copy.deepcopy(self.trainer.model)
            if self.basename is not None:
                save_trainer_state(self.basename, self.trainer.model, self.trainer.metrics)
                if self.verbose > 1:
                    print('Model saved to', self.basename + '.model')


class PrintCallback(Callback):

    def __init__(self):
        super().__init__()

    def on_train_begin(self, n_epochs, metrics):
        print('Start training for {} epochs'.format(n_epochs))

    def on_train_end(self, n_epochs, metrics):
        n_train = len(metrics['train']['losses'])
        print('Stop training at epoch: {}/{}'.format(n_train, self.trainer.last_epoch + n_epochs))

    def on_epoch_begin(self, epoch, metrics):
        self.t0 = time.time()
        self.lrs = [group['lr'] for group in self.trainer.optimizer.state_dict()['param_groups']]

    def on_epoch_end(self, epoch, metrics):
            etime = time.time() - self.t0

            print(f'{epoch:3d} (LRs: {self.lrs[0]:.2e}): {etime:5.1f}s   T:', end=' ')
            for metric_name, metric_values in metrics['train'].items():
                metric_value = metric_values[-1]
                if metric_value is not None:
                    print(f'{metric_value:.5f}', end=' ')
                    if epoch == int(np.argmin(metrics['train'][metric_name])) + 1:
                        print('*', end='  ')
                    else:
                        print(' ', end='  ')

            if len(metrics['valid']) > 0:
                print(f' V:', end=' ')
                for metric_name, metric_values in metrics['valid'].items():
                    metric_value = metric_values[-1]
                    if metric_value is not None:
                        print(f'{metric_value:.5f}', end=' ')
                        if epoch == int(np.argmin(metrics['valid'][metric_name])) + 1:
                            print('*', end='  ')
                        else:
                            print(' ', end='  ')
            print()
            
    def on_batch_end(self, epoch, batch, x, y, y_pred, loss):
        # print each batch, overwriting on the same line,  RAL 25ago2018
        if self.trainer.n_batches < 4 or batch % (self.trainer.n_batches // 4) == 0:
            print('Batch end epoch {} batch {} of {}'.format(epoch,batch,self.trainer.n_batches),end='\r')
        pass


class PlotCallback(Callback):
    def __init__(self, interval=1, max_loss=None):
        super().__init__()
        self.interval = interval
        self.max_loss = max_loss

    def on_train_begin(self, n_epochs, metrics):
        self.line_train = None
        self.line_valid = None
        self.dot_train = None
        self.dot_valid = None

        self.fig = plt.figure(figsize=(15, 6))
        self.ax = self.fig.add_subplot(1, 1, 1)
        self.ax.grid(True)

        self.plot_losses(self.trainer.metrics['train']['losses'],
                         self.trainer.metrics['valid']['losses'])

    def on_epoch_end(self, epoch, metrics):
        if epoch % self.interval == 0:
            display.clear_output(wait=True)
            self.plot_losses(self.trainer.metrics['train']['losses'],
                             self.trainer.metrics['valid']['losses'])

    def plot_losses(self, htrain, hvalid):
        epoch = len(htrain)
        if epoch == 0:
            return

        x = np.arange(1, epoch + 1)
        if self.line_train:
            self.line_train.remove()
        if self.dot_train:
            self.dot_train.remove()
        self.line_train, = self.ax.plot(x, htrain, color='#1f77b4', linewidth=2, label='training loss')
        best_epoch = int(np.argmin(htrain)) + 1
        best_loss = htrain[best_epoch - 1]
        self.dot_train = self.ax.scatter(best_epoch, best_loss, c='#1f77b4', marker='o')

        btloss = best_loss
        ctloss = htrain[-1]
        bvloss = 0.0
        cvloss = 0.0

        if hvalid[0] is not None:
            if self.line_valid:
                self.line_valid.remove()
            if self.dot_valid:
                self.dot_valid.remove()
            self.line_valid, = self.ax.plot(x, hvalid, color='#ff7f0e', linewidth=2, label='validation loss')
            best_epoch = int(np.argmin(hvalid)) + 1
            best_loss = hvalid[best_epoch - 1]
            self.dot_valid = self.ax.scatter(best_epoch, best_loss, c='#ff7f0e', marker='o')
            bvloss = best_loss
            cvloss = hvalid[-1]

        lr = self.trainer.optimizer.param_groups[0]['lr']

        self.ax.legend()
        # self.ax.vlines(best_epoch, *self.ax.get_ylim(), colors='#EBDDE2', linestyles='dashed')
        self.ax.set_title('Best epoch: {}, losses: {:.3f}/{:.3f}'
                          ' -- Current epoch: {}, losses: {:.3f}/{:.3f}'
                          ' -- lr: {:.1e}'
                          .format(best_epoch, btloss, bvloss, epoch, ctloss, cvloss, lr))

        display.display(self.fig)
        time.sleep(0.1)


def plot_losses(htrain, hvalid):
    fig = plt.figure(figsize=(15, 6))
    ax = fig.add_subplot(1, 1, 1)
    ax.grid(True)

    epoch = len(htrain)
    x = np.arange(1, epoch + 1)

    best_epoch = int(np.argmin(htrain)) + 1
    best_loss = htrain[best_epoch - 1]
    ax.plot(x, htrain, color='#1f77b4', linewidth=2, label='training loss')
    ax.scatter(best_epoch, best_loss, c='#1f77b4', marker='o')

    if hvalid[0] is not None:
        best_epoch = int(np.argmin(hvalid)) + 1
        best_loss = hvalid[best_epoch - 1]
        ax.plot(x, hvalid, color='#ff7f0e', linewidth=2, label='validation loss')
        ax.scatter(best_epoch, best_loss, c='#ff7f0e', marker='o')

    ax.legend()
    ax.set_title('Best epoch: {}, Current epoch: {}'.format(best_epoch, epoch))


class VisdomCallback(Callback):
    def __init__(self, interval=1, max_loss=None):
        super().__init__()
        self.interval = interval
        self.max_loss = max_loss

    def on_train_begin(self, n_epochs, metrics):
        self.line_train = None
        self.line_valid = None
        self.dot_train = None
        self.dot_valid = None

        self.fig = plt.figure(figsize=(15, 6))
        self.ax = self.fig.add_subplot(1, 1, 1)
        self.ax.grid(True)

        self.plot_losses(self.trainer.metrics['train']['losses'],
                         self.trainer.metrics['valid']['losses'])

    def on_epoch_end(self, epoch, metrics):
        if epoch % self.interval == 0:
            display.clear_output(wait=True)
            self.plot_losses(self.trainer.metrics['train']['losses'],
                             self.trainer.metrics['valid']['losses'])

    def plot_losses(self, htrain, hvalid):
        epoch = len(htrain)
        if epoch == 0:
            return

        x = np.arange(1, epoch + 1)
        if self.line_train:
            self.line_train.remove()
        if self.dot_train:
            self.dot_train.remove()
        self.line_train, = self.ax.plot(x, htrain, color='#1f77b4', linewidth=2, label='training loss')
        best_epoch = int(np.argmin(htrain)) + 1
        best_loss = htrain[best_epoch - 1]
        self.dot_train = self.ax.scatter(best_epoch, best_loss, c='#1f77b4', marker='o')

        if hvalid[0] is not None:
            if self.line_valid:
                self.line_valid.remove()
            if self.dot_valid:
                self.dot_valid.remove()
            self.line_valid, = self.ax.plot(x, hvalid, color='#ff7f0e', linewidth=2, label='validation loss')
            best_epoch = int(np.argmin(hvalid)) + 1
            best_loss = hvalid[best_epoch - 1]
            self.dot_valid = self.ax.scatter(best_epoch, best_loss, c='#ff7f0e', marker='o')

        self.ax.legend()
        # self.ax.vlines(best_epoch, *self.ax.get_ylim(), colors='#EBDDE2', linestyles='dashed')
        self.ax.set_title('Best epoch: {}, Current epoch: {}'.format(best_epoch, epoch))

        display.display(self.fig)
        time.sleep(0.1)

        
        
class SGDRestarts(_LRScheduler, Callback):
    """Implements Stochastic Gradient Descent with Warm Restarts as a LRScheduler.
    It is also a Callback, since it must be called on every batch, instead of after
    every epoch.
    Paper: https://arxiv.org/abs/1608.03983
    """
    def __init__(self, optimizer, last_epoch, eta_min, To, Tmul=1, n_batches=None, verbose=False):
        assert eta_min > 0
        assert To > 0
        assert Tmul > 0
        self.eta_min = eta_min
        self.To = To
        self.Ti = To
        self.Tmul = Tmul
        self.restarts = 0
        self.Tcur = 0
        
        self._n_batches = n_batches
        self.verbose = verbose
        self.history = []
        
        super().__init__(optimizer, last_epoch)
        
    @property
    def n_batches(self):
        if self._n_batches is not None:
            return self._n_batches
        return self.trainer.n_batches
        
    def plot(self, nepochs, nbatches):
        x = []
        y = []
        for i in range(1, nepochs+1):
            for j in range(nbatches):
                x.append(i + j/nbatches)
                y.append(self.on_batch_end(i, j))
                
        plt.plot(x, y)
        plt.grid()
        plt.xticks(np.arange(1, nepochs+2))
        
    
    def get_lr(self, lr, cepoch, cbatch):
        # Return learning rates for current epoch and batch
        if self.last_epoch == -1:
            # First epoch
            self.last_epoch = cepoch
        elif cepoch > self.last_epoch:
            # New epoch. Increase epochs performed since last restart
            self.last_epoch = cepoch
            self.Tcur += 1
            
        if self.Tcur == self.Ti:
            # Restart learning rate
            if self.verbose:
                print('\nRestarting learning rates.\n')
            self.Tcur = 0
            self.restarts += 1
            # New cosine period
            self.Ti = self.To * (self.Tmul**self.restarts)
            
        # Step: calculate step, in radians, so that the first batch after restart has
        # learning rate `lr` and the last batch before restart has eta_min
        step = self.Ti / max(1, ((self.Ti * self.n_batches) - 1))
        x = step * (self.Tcur*self.n_batches + cbatch)
    
#         return [self.eta_min + (lr - self.eta_min) * (1 + math.cos(x*math.pi/self.Ti)) / 2
#                 for lr in self.base_lrs]
        return self.eta_min + (lr - self.eta_min) * (1 + math.cos(x*math.pi/self.Ti)) / 2


    def step(self, epoch, batch=None):
        if batch is None:
            return
        if epoch is None:
            epoch = self.last_epoch + 1
#         for param_group, lr in zip(self.optimizer.param_groups,
#                                    self.get_lr(epoch, batch)):
#             param_group['lr'] = lr
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = self.get_lr(param_group['initial_lr'], epoch, batch)
        self.last_epoch = epoch
        
        
    def on_train_begin(self, n_epochs, metrics):
        self.last_epoch = self.trainer.last_epoch
        
    def on_batch_end(self, *args):
        pass
#         self.history.append(self.optimizer.param_groups[0]['lr'])
   
    
    def on_batch_begin(self, cepoch, cbatch, *args):
        # import pdb; pdb.set_trace()
        self.history.append(self.optimizer.param_groups[0]['lr'])
        self.step(cepoch, cbatch)
        

        