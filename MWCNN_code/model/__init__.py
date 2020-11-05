import os
from importlib import import_module

import torch
import torch.nn as nn
from torch.autograd import Variable

class Model(nn.Module):
    def __init__(self, args, ckp):
        """
        Initialize the model.

        Args:
            self: (todo): write your description
            ckp: (int): write your description
        """
        super(Model, self).__init__()
        print('Making model...')

        self.scale = args.scale
        self.idx_scale = 0
        self.self_ensemble = args.self_ensemble
        self.chop = args.chop
        self.precision = args.precision
        self.cpu = args.cpu
        self.device = torch.device('cpu' if args.cpu else 'cuda')
        self.n_GPUs = args.n_GPUs
        self.save_models = args.save_models

        module = import_module('model.' + args.model.lower())
        self.model = module.make_model(args).to(self.device)
        if args.precision == 'half': self.model.half()

        if not args.cpu and args.n_GPUs > 1:
            self.model = nn.DataParallel(self.model, range(args.n_GPUs))

        self.load(
            ckp.dir,
            pre_train=args.pre_train,
            resume=args.resume,
            name=args.model,
            cpu=args.cpu
        )
        if args.print_model: print(self.model)

    def forward(self, x, idx_scale):
        """
        Forward computation.

        Args:
            self: (todo): write your description
            x: (todo): write your description
            idx_scale: (todo): write your description
        """
        self.idx_scale = idx_scale
        target = self.get_model()
        if hasattr(target, 'set_scale'):
            target.set_scale(idx_scale)

        if self.self_ensemble and not self.training:
            if self.chop:
                forward_function = self.forward_chop
            else:
                forward_function = self.model.forward

            return self.forward_x8(x, forward_function)
        elif self.chop and not self.training:
            return self.forward_chop(x)
            # return self.model(x)
        else:

            return self.model(x)

    def get_model(self):
        """
        Gets the model.

        Args:
            self: (todo): write your description
        """
        if self.n_GPUs == 1:
            return self.model
        else:
            return self.model.module

    def state_dict(self, **kwargs):
        """
        Get the state dictionary.

        Args:
            self: (todo): write your description
        """
        target = self.get_model()
        return target.state_dict(**kwargs)

    def save(self, apath, epoch, name, is_best=False):
        """
        Saves the state of the model.

        Args:
            self: (todo): write your description
            apath: (str): write your description
            epoch: (int): write your description
            name: (str): write your description
            is_best: (bool): write your description
        """
        target = self.get_model()
        torch.save(
            target.state_dict(), 
            os.path.join(apath, 'model', name + 'model_latest.pt')
        )
        if is_best:
            torch.save(
                target.state_dict(),
                os.path.join(apath, 'model', name + 'model_best.pt')
            )
        
        if self.save_models:
            torch.save(
                target.state_dict(),
                os.path.join(apath, 'model', name + 'model_{}.pt'.format(epoch))
            )

    def load(self, apath, pre_train='.', resume=-1, name='',  cpu=False):
        """
        Load the model from the given apath.

        Args:
            self: (todo): write your description
            apath: (str): write your description
            pre_train: (bool): write your description
            resume: (bool): write your description
            name: (str): write your description
            cpu: (str): write your description
        """
        if cpu:
            kwargs = {'map_location': lambda storage, loc: storage}
        else:
            kwargs = {}

        if resume == -1:
            self.get_model().load_state_dict(
                torch.load(
                    os.path.join(pre_train, name + 'model_latest.pt'),
                    **kwargs
                ),
                strict=False
            )

            # self.get_model().load_state_dict(
            #     torch.load(
            #         os.path.join(apath, 'model', name + 'model_latest.pt'),
            #         **kwargs
            #     ),
            #     strict=False
            # )

        elif resume == 0:
            if pre_train != '.':
                print('Loading model from {}'.format(pre_train))
                self.get_model().load_state_dict(
                    torch.load(pre_train, **kwargs),
                    strict=False
                )
        else:
            self.get_model().load_state_dict(
                torch.load(
                    os.path.join(apath, 'model', 'model_{}.pt'.format(resume)),
                    **kwargs
                ),
                strict=False
            )

    def forward_chop(self, x, shave=10, min_size=160000):
        """
        Forward computation.

        Args:
            self: (todo): write your description
            x: (todo): write your description
            shave: (todo): write your description
            min_size: (int): write your description
        """
        scale = self.scale[self.idx_scale]
        n_GPUs = min(self.n_GPUs, 4)
        b, c, h, w = x.size()
        h_half, w_half = h // 2, w // 2
        h_size, w_size = h_half + shave, w_half + shave
        lr_list = [
            x[:, :, 0:h_size, 0:w_size],
            x[:, :, 0:h_size, (w - w_size):w],
            x[:, :, (h - h_size):h, 0:w_size],
            x[:, :, (h - h_size):h, (w - w_size):w]]
        # lr_list = [
        #     x[:, :, 0:h_size, 0:w_size],
        #     x[:, :, 0:h_size, (w - w_size):w],
        #     x[:, :, (h - h_size):h, 0:w_size],
        #     x[:, :, (h - h_size):h, (w - w_size):w]]

        if w_size * h_size < min_size:
            sr_list = []
            for i in range(0, 4, n_GPUs):
                lr_batch = torch.cat(lr_list[i:(i + n_GPUs)], dim=0)
                sr_batch = self.model(lr_batch)
                sr_list.extend(sr_batch.chunk(n_GPUs, dim=0))
        else:
            sr_list = [
                self.forward_chop(patch, shave=shave, min_size=min_size) \
                for patch in lr_list
            ]

        h, w = scale * h, scale * w
        h_half, w_half = scale * h_half, scale * w_half
        h_size, w_size = scale * h_size, scale * w_size
        shave *= scale

        output = x.new(b, c, h, w)
        output[:, :, 0:h_half, 0:w_half] \
            = sr_list[0][:, :, 0:h_half, 0:w_half]
        output[:, :, 0:h_half, w_half:w] \
            = sr_list[1][:, :, 0:h_half, (w_size - w + w_half):w_size]
        output[:, :, h_half:h, 0:w_half] \
            = sr_list[2][:, :, (h_size - h + h_half):h_size, 0:w_half]
        output[:, :, h_half:h, w_half:w] \
            = sr_list[3][:, :, (h_size - h + h_half):h_size, (w_size - w + w_half):w_size]

        return output

    def forward_x8(self, x, forward_function):
        """
        Perform forward computation.

        Args:
            self: (todo): write your description
            x: (todo): write your description
            forward_function: (todo): write your description
        """
        def _transform(v, op):
            """
            Transform an op to a tensor.

            Args:
                v: (array): write your description
                op: (array): write your description
            """
            if self.precision != 'single': v = v.float()

            v2np = v.data.cpu().numpy()
            if op == 'v':
                tfnp = v2np[:, :, :, ::-1].copy()
            elif op == 'h':
                tfnp = v2np[:, :, ::-1, :].copy()
            elif op == 't':
                tfnp = v2np.transpose((0, 1, 3, 2)).copy()

            ret = torch.Tensor(tfnp).to(self.device)
            if self.precision == 'half': ret = ret.half()

            return ret

        lr_list = [x]
        for tf in 'v', 'h', 't':
            lr_list.extend([_transform(t, tf) for t in lr_list])

        sr_list = [forward_function(aug) for aug in lr_list]
        for i in range(len(sr_list)):
            if i > 3:
                sr_list[i] = _transform(sr_list[i], 't')
            if i % 4 > 1:
                sr_list[i] = _transform(sr_list[i], 'h')
            if (i % 4) % 2 == 1:
                sr_list[i] = _transform(sr_list[i], 'v')

        output_cat = torch.cat(sr_list, dim=0)
        output = output_cat.mean(dim=0, keepdim=True)

        return output

