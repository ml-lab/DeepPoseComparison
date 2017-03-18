# -*- coding: utf-8 -*-
""" Train pose net. """

import os
from tqdm import tqdm, trange
import torch
import torch.optim as optim
from torch.autograd import Variable
from torchvision import transforms

from modules.errors import FileNotFoundError, GPUNotFoundError, UnknownOptimizationMethodError
from modules.models.pytorch import AlexNet
from modules.dataset_indexing.pytorch import PoseDataset, Crop, RandomNoise, Scale
from modules.functions.pytorch import mean_squared_error


class TrainLogger(object):
    """ Logger of training pose net.

    Args:
        out (str): Output directory.
    """

    def __init__(self, out):
        try:
            os.makedirs(out)
        except OSError:
            pass
        self.file = open(os.path.join(out, 'log'), 'w')
        self.logs = []

    def write(self, log):
        """ Write log. """
        tqdm.write(log)
        tqdm.write(log, file=self.file)
        self.logs.append(log)

    def state_dict(self):
        """ Returns the state of the logger. """
        return {'logs': self.logs}

    def load_state_dict(self, state_dict):
        """ Loads the logger state. """
        self.logs = state_dict['logs']
        # write logs.
        tqdm.write(self.logs[-1])
        for log in self.logs:
            tqdm.write(log, file=self.file)

# pylint: disable=too-many-instance-attributes
class TrainPoseNet(object):
    """ Train pose net of estimating 2D pose from image.

    Args:
        Nj (int): Number of joints.
        use_visibility (bool): Use visibility to compute loss.
        epoch (int): Number of epochs to train.
        opt (str): Optimization method.
        gpu (bool): Use GPU.
        train (str): Path to training image-pose list file.
        val (str): Path to validation image-pose list file.
        batchsize (int): Learning minibatch size.
        out (str): Output directory.
        resume (str): Initialize the trainer from given file.
            The file name is 'epoch-{epoch number}.iter'.
        resume_model (str): Load model definition file to use for resuming training
            (it\'s necessary when you resume a training).
            The file name is 'epoch-{epoch number}.model'.
        resume_opt (str): Load optimization states from this file
            (it\'s necessary when you resume a training).
            The file name is 'epoch-{epoch number}.state'.
    """

    # pylint: disable=too-many-arguments
    def __init__(self, **kwargs):
        self.Nj = kwargs['Nj']
        self.use_visibility = kwargs['use_visibility']
        self.epoch = kwargs['epoch']
        self.gpu = (kwargs['gpu'] >= 0)
        self.opt = kwargs['opt']
        self.train = kwargs['train']
        self.val = kwargs['val']
        self.batchsize = kwargs['batchsize']
        self.out = kwargs['out']
        self.resume = kwargs['resume']
        self.resume_model = kwargs['resume_model']
        self.resume_opt = kwargs['resume_opt']
        # validate arguments.
        self._validate_arguments()

    def _validate_arguments(self):
        if self.gpu and not torch.cuda.is_available():
            raise GPUNotFoundError('GPU is not found.')
        for path in (self.train, self.val):
            if not os.path.isfile(path):
                raise FileNotFoundError('{0} is not found.'.format(path))
        if self.opt not in ('MomentumSGD', 'Adam'):
            raise UnknownOptimizationMethodError(
                '{0} is unknown optimization method.'.format(self.opt))
        if self.resume is not None:
            for path in (self.resume, self.resume_model, self.resume_opt):
                if not os.path.isfile(path):
                    raise FileNotFoundError('{0} is not found.'.format(path))

    def _get_optimizer(self, model):
        if self.opt == 'MomentumSGD':
            optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
        elif self.opt == "Adam":
            optimizer = optim.Adam(model.parameters())
        return optimizer

    def _train(self, model, optimizer, train_iter, log_interval, logger):
        model.train()
        for iteration, batch in enumerate(tqdm(train_iter, desc='this epoch')):
            image, pose, visibility = Variable(batch[0]), Variable(batch[1]), Variable(batch[2])
            if self.gpu:
                image, pose, visibility = image.cuda(), pose.cuda(), visibility.cuda()
            optimizer.zero_grad()
            output = model(image)
            loss = mean_squared_error(output, pose, visibility, self.use_visibility)
            loss.backward()
            optimizer.step()
            if iteration % log_interval == 0:
                log = 'Loss: {}'.format(loss.data[0])
                logger.write(log)

    def _test(self, model, test_iter, logger):
        model.eval()
        test_loss = 0
        for batch in test_iter:
            image, pose, visibility = Variable(batch[0]), Variable(batch[1]), Variable(batch[2])
            if self.gpu:
                image, pose, visibility = image.cuda(), pose.cuda(), visibility.cuda()
            output = model(image)
            test_loss += mean_squared_error(output, pose, visibility, self.use_visibility).data[0]
        test_loss /= len(test_iter)
        log = 'Validation/Loss: {}'.format(test_loss)
        logger.write(log)

    def _checkpoint(self, epoch, model, optimizer, logger):
        filename = os.path.join(self.out, 'pytorch', 'epoch-{0}'.format(epoch))
        torch.save({'epoch': epoch + 1, 'logger': logger.state_dict()}, filename + '.iter')
        torch.save(model.state_dict(), filename + '.model')
        torch.save(optimizer.state_dict(), filename + '.state')

    def start(self):
        """ Train pose net. """
        # initialize model to train.
        model = AlexNet(self.Nj)
        if self.resume_model:
            model.load_state_dict(torch.load(self.resume_model))
        # prepare gpu.
        if self.gpu:
            model.cuda()
        # load the datasets.
        train = PoseDataset(
            self.train,
            input_transform=transforms.Compose([
                transforms.ToTensor(),
                RandomNoise()]),
            output_transform=Scale(),
            transform=Crop(data_augmentation=True))
        val = PoseDataset(
            self.val,
            input_transform=transforms.Compose([
                transforms.ToTensor()]),
            output_transform=Scale(),
            transform=Crop(data_augmentation=False))
        # training/validation iterators.
        train_iter = torch.utils.data.DataLoader(train, batch_size=self.batchsize, shuffle=True)
        val_iter = torch.utils.data.DataLoader(val, batch_size=self.batchsize, shuffle=False)
        # set up an optimizer.
        optimizer = self._get_optimizer(model)
        if self.resume_opt:
            optimizer.load_state_dict(torch.load(self.resume_opt))
        # set intervals.
        val_interval = 10
        resume_interval = self.epoch/10
        log_interval = 10
        # set logger and start epoch.
        logger = TrainLogger(os.path.join(self.out, 'pytorch'))
        start_epoch = 1
        if self.resume:
            resume = torch.load(self.resume)
            start_epoch = resume['epoch']
            logger.load_state_dict(resume['logger'])
        # start training.
        for epoch in trange(start_epoch, self.epoch + 1, desc='     total'):
            self._train(model, optimizer, train_iter, log_interval, logger)
            if epoch % val_interval == 0:
                self._test(model, val_iter, logger)
            if epoch % resume_interval == 0:
                self._checkpoint(epoch, model, optimizer, logger)