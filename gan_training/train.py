# coding: utf-8
import torch
from torch.nn import functional as F
import torch.utils.data
import torch.utils.data.distributed
from torch import autograd
from .lap import *

class Trainer(object):
    def __init__(self, generator, discriminator, g_optimizer, d_optimizer,
                 gan_type, reg_type, reg_param, reg_lap):
        self.generator = generator
        self.discriminator = discriminator
        self.g_optimizer = g_optimizer
        self.d_optimizer = d_optimizer

        self.gan_type = gan_type
        self.reg_type = reg_type
        self.reg_param = reg_param
        self.reg_lap = reg_lap
        if self.reg_lap != 0:
            self.lap = lap_pyramid(6, 128)

    def generator_trainstep(self, y, z, x_real):
        assert(y.size(0) == z.size(0))
        toogle_grad(self.generator, True)
        toogle_grad(self.discriminator, False)
        self.generator.train()
        self.discriminator.train()
        self.g_optimizer.zero_grad()
        x_fake = self.generator(z, y)
        d_fake = self.discriminator(x_fake, y)
        if self.reg_lap != 0:
            g_reg = self.lap.compare(x_real, x_fake)*self.reg_lap
        else:
            g_reg = torch.Tensor([0]).type(torch.cuda.FloatTensor)
        gloss = self.compute_loss(d_fake, 1) + g_reg
        gloss.backward()

        self.g_optimizer.step()

        return gloss.item(), g_reg.item()

    def discriminator_trainstep(self, x_real, y, z):
        toogle_grad(self.generator, False)
        toogle_grad(self.discriminator, True)
        self.generator.train()
        self.discriminator.train()
        self.d_optimizer.zero_grad()

        # On real data
        x_real.requires_grad_()

        d_real = self.discriminator(x_real, y)
        dloss_real = self.compute_loss(d_real, 1)

        if self.reg_type == 'real':
            dloss_real.backward(retain_graph=True)
            reg = self.reg_param * compute_grad2(d_real, x_real).mean()
            reg.backward()
        else:
            dloss_real.backward()

        # On fake data
        with torch.no_grad():
            x_fake = self.generator(z, y)

        x_fake.requires_grad_()
        d_fake = self.discriminator(x_fake, y)
        dloss_fake = self.compute_loss(d_fake, 0)

        if self.reg_type == 'fake':
            dloss_fake.backward(retain_graph=True)
            reg = self.reg_param * compute_grad2(d_fake, x_fake).mean()
            reg.backward()
        else:
            dloss_fake.backward()

        if self.reg_type == 'wgangp':
            reg = self.reg_param * self.wgan_gp_reg(x_real, x_fake, y)
            reg.backward()

        self.d_optimizer.step()

        toogle_grad(self.discriminator, False)

        # Output
        dloss = (dloss_real + dloss_fake)

        if self.reg_type == 'none':
            reg = torch.tensor(0.)

        return dloss.item(), reg.item()

    def compute_loss(self, d_out, target):
        targets = d_out.new_full(size=d_out.size(), fill_value=target)

        if self.gan_type == 'standard':
            loss = F.binary_cross_entropy_with_logits(d_out, targets)
        elif self.gan_type == 'wgan':
            loss = (2*target - 1) * d_out.mean()
        else:
            raise NotImplementedError

        return loss

    def wgan_gp_reg(self, x_real, x_fake, y):
        batch_size = y.size(0)
        eps = torch.rand(batch_size, device=y.device).view(batch_size, 1, 1, 1)
        x_interp = (1 - eps) * x_real + eps * x_fake
        x_interp = x_interp.detach()
        x_interp.requires_grad_()
        d_out = self.discriminator(x_interp, y)

        reg = (compute_grad2(d_out, x_interp).sqrt() - 1.).pow(2).mean()

        return reg


# Utility functions
def toogle_grad(model, requires_grad):
    for p in model.parameters():
        p.requires_grad_(requires_grad)


def compute_grad2(d_out, x_in):
    batch_size = x_in.size(0)
    grad_dout = autograd.grad(
        outputs=d_out.sum(), inputs=x_in,
        create_graph=True, retain_graph=True, only_inputs=True
    )[0]
    grad_dout2 = grad_dout.pow(2)
    assert(grad_dout2.size() == x_in.size())
    reg = grad_dout2.view(batch_size, -1).sum(1)
    return reg


def update_average(model_tgt, model_src, beta):
    toogle_grad(model_src, False)
    toogle_grad(model_tgt, False)

    param_dict_src = dict(model_src.named_parameters())

    for p_name, p_tgt in model_tgt.named_parameters():
        p_src = param_dict_src[p_name]
        assert(p_src is not p_tgt)
        p_tgt.copy_(beta*p_tgt + (1. - beta)*p_src)
