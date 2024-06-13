"""Utility functions related to Bayesian inversion."""
from enum import Enum

import jax.numpy as jnp
from jax import grad, jacfwd, jacrev, vjp, vmap

from diffusionlib.util.misc import (
    batch_linalg_solve,
    batch_linalg_solve_A,
    batch_matmul,
    batch_matmul_A,
    batch_mul,
    batch_mul_A,
)

__CONDITIONING_METHOD__ = {}


class ConditioningMethodName(str, Enum):
    DIFFUSION_POSTERIOR_SAMPLING = "diffusion_posterior_sampling"
    DIFFUSION_POSTERIOR_SAMPLING_MOD = "diffusion_psoterior_sampling_mod"
    PSEUDO_INVERSE_GUIDANCE = "pseduo_inverse_guidance"
    VJP_GUIDANCE = "vjp_guidance"
    VJP_GUIDANCE_ALT = "vjp_guidance_alt"
    VJP_GUIDANCE_MASK = "vjp_guidance_mask"
    VJP_GUIDANCE_DIAG = "vjp_guidance_diag"
    JAC_REV_GUIDANCE = "jac_rev_guidance"
    JAC_REV_GUIDANCE_DIAG = "jac_rev_guidance_diag"
    JAC_FWD_GUIDANCE = "jac_fwd_guidance"
    JAC_FWD_GUIDANCE_DIAG = "jac_fwd_guidance_diag"


def register_conditioning_method(name: ConditioningMethodName):
    def wrapper(cls):
        if __CONDITIONING_METHOD__.get(name, None):
            raise NameError(f"Name {name} is already registered!")
        __CONDITIONING_METHOD__[name] = cls
        return cls

    return wrapper


def get_conditioning_method(name: ConditioningMethodName, *args, **kwargs):
    if __CONDITIONING_METHOD__.get(name, None) is None:
        raise NameError(f"Name {name} is not defined!")
    return __CONDITIONING_METHOD__[name](*args, **kwargs)


@register_conditioning_method(ConditioningMethodName.DIFFUSION_POSTERIOR_SAMPLING)
class DPS:
    """
    Implementation of score guidance suggested in
    `Diffusion Posterior Sampling for general noisy inverse problems'
    Chung et al. 2022,
    https://github.com/DPS2022/diffusion-posterior-sampling/blob/main/guided_diffusion/condition_methods.py

    Computes a single (batched) gradient.

    NOTE: This is not how Chung et al. 2022 implemented their method, but is a related
    continuous time method.

    Args:
        scale: Hyperparameter of the method.
            See https://arxiv.org/pdf/2209.14687.pdf#page=20&zoom=100,144,757
    """

    def __init__(self, sde, observation_map, y, scale=0.4):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.scale = scale

    def get_guidance_score_func(self):
        def get_l2_norm(y, estimate_h_x_0):
            def l2_norm(x, t):
                h_x_0, (s, _) = estimate_h_x_0(x, t)
                innovation = y - h_x_0
                return jnp.linalg.norm(innovation), s

            return l2_norm

        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)
        l2_norm = get_l2_norm(self.y, estimate_h_x_0)
        likelihood_score = grad(l2_norm, has_aux=True)

        def guidance_score(x, t):
            ls, s = likelihood_score(x, t)
            gs = s - self.scale * ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.DIFFUSION_POSTERIOR_SAMPLING_MOD)
class DPSMod:
    """
    Implementation of score guidance suggested in
    `Diffusion Posterior Sampling for general noisy inverse problems'
    Chung et al. 2022,
    https://github.com/DPS2022/diffusion-posterior-sampling/blob/main/guided_diffusion/condition_methods.py
    guidance score for an observation_map that can be
    represented by either `def observation_map(x: Float[Array, dims]) -> y: Float[Array, d_x = dims.flatten()]: return mask * x  # (d_x,)`
        or `def observation_map(x: Float[Array, dims]) -> y: Float[Array, d_y]: return H @ x   # (d_y,)`
    Computes one vjps.

    NOTE: This is not how Chung et al. 2022 implemented their method, their method is `:meth:get_dps`.
    Whereas this method uses their approximation in Eq. 11 https://arxiv.org/pdf/2209.14687.pdf#page=20&zoom=100,144,757
    to directly calculate the score.
    """

    def __init__(self, sde, observation_map, y, noise_std):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.noise_std = noise_std

    def get_guidance_score_func(self):
        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)

        def guidance_score(x, t):
            h_x_0, vjp_estimate_h_x_0, (s, _) = vjp(lambda x: estimate_h_x_0(x, t), x, has_aux=True)
            innovation = self.y - h_x_0
            C_yy = (
                self.noise_std**2
            )  # TODO: could investigate replacing with jnp.linalg.norm(innovation**2)
            ls = innovation / C_yy
            ls = vjp_estimate_h_x_0(ls)[0]
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.PSEUDO_INVERSE_GUIDANCE)
class PIG:
    """
    `Pseudo-Inverse guided diffusion models for inverse problems`
    https://openreview.net/pdf?id=9_gsMA8MRKQ
    Song et al. 2023,
    guidance score for an observation_map that can be
    represented by either `def observation_map(x: Float[Array, dims]) -> y: Float[Array, d_x = dims.flatten()]: return mask * x  # (d_x,)`
        or `def observation_map(x: Float[Array, dims]) -> y: Float[Array, d_y]: return H @ x   # (d_y,)`
    Computes one vjps.
    """

    def __init__(self, sde, observation_map, y, noise_std, HHT=jnp.array([1.0])):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.noise_std = noise_std
        self.HHT = HHT

    def get_guidance_score_func(self):
        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)

        def guidance_score(x, t):
            h_x_0, vjp_estimate_h_x_0, (s, _) = vjp(lambda x: estimate_h_x_0(x, t), x, has_aux=True)
            innovation = self.y - h_x_0
            if self.HHT.shape == (self.y.shape[1], self.y.shape[1]):
                C_yy = self.sde.r2(
                    t[0], data_variance=1.0
                ) * self.HHT + self.noise_std**2 * jnp.eye(self.y.shape[1])
                f = batch_linalg_solve_A(C_yy, innovation)
            elif self.HHT.shape == (1,):
                C_yy = self.sde.r2(t[0], data_variance=1.0) * self.HHT + self.noise_std**2
                f = innovation / C_yy
            ls = vjp_estimate_h_x_0(f)[0]
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.VJP_GUIDANCE_ALT)
class VJPAlt:
    """
    Uses full second moment approximation of the covariance of x_0|x_t.

    Computes using H.shape[0] vjps.

    NOTE: Alternate implementation to `meth:get_vjp_guidance` that does all reshaping here.
    """

    def __init__(self, sde, H, y, noise_std, shape):
        self.sde = sde
        self.H = H
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        estimate_x_0 = self.sde.get_estimate_x_0(lambda x: x)
        _shape = (self.H.shape[0],) + self.shape[1:]
        axes = (1, 0) + tuple(range(len(self.shape) + 1)[2:])
        batch_H = jnp.transpose(
            jnp.tile(self.H.reshape(_shape), (self.shape[0],) + len(self.shape) * (1,)), axes=axes
        )

        def guidance_score(x, t):
            x_0, vjp_x_0, (s, _) = vjp(lambda x: estimate_x_0(x, t), x, has_aux=True)
            vec_vjp_x_0 = vmap(vjp_x_0)
            H_grad_x_0 = vec_vjp_x_0(batch_H)[0]
            H_grad_x_0 = H_grad_x_0.reshape(self.H.shape[0], self.shape[0], self.H.shape[1])
            C_yy = self.sde.ratio(t[0]) * batch_matmul_A(
                self.H, H_grad_x_0.transpose(1, 2, 0)
            ) + self.noise_std**2 * jnp.eye(self.y.shape[1])
            innovation = self.y - batch_matmul_A(self.H, x_0.reshape(self.shape[0], -1))
            f = batch_linalg_solve(C_yy, innovation)
            ls = vjp_x_0(batch_matmul_A(self.H.T, f).reshape(self.shape))[0]
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.VJP_GUIDANCE)
class VJP:
    """
    Uses full second moment approximation of the covariance of x_0|x_t.

    Computes using H.shape[0] vjps.
    """

    def __init__(self, sde, H, y, noise_std, shape):
        self.sde = sde
        self.H = H
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        # TODO: necessary to use shape here?
        estimate_x_0 = self.sde.get_estimate_x_0(lambda x: x, shape=(self.shape[0], -1))
        batch_H = jnp.transpose(jnp.tile(self.H, (self.shape[0], 1, 1)), axes=(1, 0, 2))

        def guidance_score(x, t):
            x_0, vjp_x_0, (s, _) = vjp(lambda x: estimate_x_0(x, t), x, has_aux=True)
            vec_vjp_x_0 = vmap(vjp_x_0)
            H_grad_x_0 = vec_vjp_x_0(batch_H)[0]
            H_grad_x_0 = H_grad_x_0.reshape(self.H.shape[0], self.shape[0], self.H.shape[1])
            C_yy = self.sde.ratio(t[0]) * batch_matmul_A(
                self.H, H_grad_x_0.transpose(1, 2, 0)
            ) + self.noise_std**2 * jnp.eye(self.y.shape[1])
            innovation = self.y - batch_matmul_A(self.H, x_0)
            f = batch_linalg_solve(C_yy, innovation)
            # NOTE: in some early tests it's faster to calculate via H_grad_x_0, instead of another vjp
            ls = batch_matmul(H_grad_x_0.transpose(1, 2, 0), f).reshape(s.shape)
            # ls = vjp_x_0(batch_matmul_A(H.T, f))[0]
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.VJP_GUIDANCE_MASK)
class VJPMask:
    """
    Uses row sum of second moment approximation of the covariance of x_0|x_t.

    Computes two vjps.
    """

    def __init__(self, sde, observation_map, y, noise_std, shape):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)
        batch_observation_map = vmap(self.observation_map)

        def guidance_score(x, t):
            h_x_0, vjp_h_x_0, (s, _) = vjp(lambda x: estimate_h_x_0(x, t), x, has_aux=True)
            diag = batch_observation_map(vjp_h_x_0(batch_observation_map(jnp.ones_like(x)))[0])
            C_yy = self.sde.ratio(t[0]) * diag + self.noise_std**2
            innovation = self.y - h_x_0
            ls = innovation / C_yy
            ls = vjp_h_x_0(ls)[0]
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.JAC_REV_GUIDANCE)
class JacRev:
    """
    Uses full second moment approximation of the covariance of x_0|x_t.

    Computes using d_y vjps.
    """

    def __init__(self, sde, observation_map, y, noise_std, shape):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        batch_batch_observation_map = vmap(vmap(self.observation_map))
        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)
        estimate_h_x_0_vmap = self.sde.get_estimate_x_0_vmap(self.observation_map)
        jacrev_vmap = vmap(jacrev(lambda x, t: estimate_h_x_0_vmap(x, t)[0]))

        # axes tuple for correct permutation of grad_H_x_0 array
        axes = (0,) + tuple(range(len(self.shape) + 1)[2:]) + (1,)

        def guidance_score(x, t):
            h_x_0, (s, _) = estimate_h_x_0(
                x, t
            )  # TODO: in python 3.8 this line can be removed by utilizing has_aux=True
            grad_H_x_0 = jacrev_vmap(x, t)
            H_grad_H_x_0 = batch_batch_observation_map(grad_H_x_0)
            C_yy = self.sde.ratio(t[0]) * H_grad_H_x_0 + self.noise_std**2 * jnp.eye(
                self.y.shape[1]
            )
            innovation = self.y - h_x_0
            f = batch_linalg_solve(C_yy, innovation)
            ls = batch_matmul(jnp.transpose(grad_H_x_0, axes), f).reshape(s.shape)
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.JAC_FWD_GUIDANCE)
class JacFwd:
    """
    Uses full second moment approximation of the covariance of x_0|x_t.

    Computes using d_y jvps.
    """

    def __init__(self, sde, observation_map, y, noise_std, shape):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        batch_batch_observation_map = vmap(vmap(self.observation_map))
        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)
        estimate_h_x_0_vmap = self.sde.get_estimate_x_0_vmap(self.observation_map)

        # axes tuple for correct permutation of grad_H_x_0 array
        axes = (0,) + tuple(range(len(self.shape) + 1)[2:]) + (1,)
        jacfwd_vmap = vmap(jacfwd(lambda x, t: estimate_h_x_0_vmap(x, t)[0]))

        def guidance_score(x, t):
            h_x_0, (s, _) = estimate_h_x_0(
                x, t
            )  # TODO: in python 3.8 this line can be removed by utilizing has_aux=True
            H_grad_x_0 = jacfwd_vmap(x, t)
            H_grad_H_x_0 = batch_batch_observation_map(H_grad_x_0)
            C_yy = self.sde.ratio(t[0]) * H_grad_H_x_0 + self.noise_std**2 * jnp.eye(
                self.y.shape[1]
            )
            innovation = self.y - h_x_0
            f = batch_linalg_solve(C_yy, innovation)
            ls = batch_matmul(jnp.transpose(H_grad_x_0, axes), f).reshape(s.shape)
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.JAC_REV_GUIDANCE_DIAG)
class JacRevDiagonal:
    """Use a diagonal approximation to the variance inside the likelihood,
    This produces similar results when the covariance is approximately diagonal
    """

    def __init__(self, sde, observation_map, y, noise_std, shape):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)
        batch_batch_observation_map = vmap(vmap(self.observation_map))

        # axes tuple for correct permutation of grad_H_x_0 array
        axes = (0,) + tuple(range(len(self.shape) + 1)[2:]) + (1,)

        def vec_jacrev(x, t):
            return vmap(
                jacrev(lambda _x: estimate_h_x_0(jnp.expand_dims(_x, axis=0), t.reshape(1, 1))[0])
            )(x)

        def guidance_score(x, t):
            h_x_0, (s, _) = estimate_h_x_0(
                x, t
            )  # TODO: in python 3.8 this line can be removed by utilizing has_aux=True
            grad_H_x_0 = jnp.squeeze(vec_jacrev(x, t[0]), axis=1)
            H_grad_H_x_0 = batch_batch_observation_map(grad_H_x_0)
            C_yy = (
                self.sde.ratio(t[0]) * jnp.diagonal(H_grad_H_x_0, axis1=1, axis2=2)
                + self.noise_std**2
            )
            innovation = self.y - h_x_0
            f = batch_mul(innovation, 1.0 / C_yy)
            ls = batch_matmul(jnp.transpose(grad_H_x_0, axes=axes), f).reshape(s.shape)
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.VJP_GUIDANCE_DIAG)
class VJPDiagonal:
    """
    Uses full second moment approximation of the covariance of x_0|x_t.

    Computes using H.shape[0] vjps.
    """

    def __init__(self, sde, H, y, noise_std, shape):
        self.sde = sde
        self.H = H
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        # TODO: necessary to use shape here?
        estimate_x_0 = self.sde.get_estimate_x_0(lambda x: x, shape=(self.shape[0], -1))
        batch_H = jnp.transpose(jnp.tile(self.H, (self.shape[0], 1, 1)), axes=(1, 0, 2))

        def guidance_score(x, t):
            x_0, vjp_x_0, (s, _) = vjp(lambda x: estimate_x_0(x, t), x, has_aux=True)
            vec_vjp_x_0 = vmap(vjp_x_0)
            H_grad_x_0 = vec_vjp_x_0(batch_H)[0]
            H_grad_x_0 = H_grad_x_0.reshape(self.H.shape[0], self.shape[0], self.H.shape[1])
            diag_H_grad_H_x_0 = jnp.sum(batch_mul_A(self.H, H_grad_x_0.transpose(1, 0, 2)), axis=-1)
            C_yy = self.sde.ratio(t[0]) * diag_H_grad_H_x_0 + self.noise_std**2
            innovation = self.y - batch_matmul_A(self.H, x_0)
            f = batch_mul(innovation, 1.0 / C_yy)
            ls = vjp_x_0(batch_matmul_A(self.H.T, f))[0]
            gs = s + ls
            return gs

        return guidance_score


@register_conditioning_method(ConditioningMethodName.JAC_FWD_GUIDANCE_DIAG)
class JacFwdDiagonal:
    """Use a diagonal approximation to the variance inside the likelihood,
    This produces similar results when the covariance is approximately diagonal
    """

    def __init__(self, sde, observation_map, y, noise_std, shape):
        self.sde = sde
        self.observation_map = observation_map
        self.y = y
        self.noise_std = noise_std
        self.shape = shape

    def get_guidance_score_func(self):
        batch_batch_observation_map = vmap(vmap(self.observation_map))
        estimate_h_x_0 = self.sde.get_estimate_x_0(self.observation_map)
        # axes tuple for correct permutation of grad_H_x_0 array
        axes = (0,) + tuple(range(len(self.shape) + 1)[2:]) + (1,)

        def vec_jacfwd(x, t):
            return vmap(
                jacfwd(lambda _x: estimate_h_x_0(jnp.expand_dims(_x, axis=0), t.reshape(1, 1))[0])
            )(x)

        def guidance_score(x, t):
            h_x_0, (s, _) = estimate_h_x_0(
                x, t
            )  # TODO: in python 3.8 this line can be removed by utilizing has_aux=True
            H_grad_x_0 = jnp.squeeze(vec_jacfwd(x, t[0]), axis=(1))
            H_grad_H_x_0 = batch_batch_observation_map(H_grad_x_0)
            C_yy = (
                self.sde.ratio(t[0]) * jnp.diagonal(H_grad_H_x_0, axis1=1, axis2=2)
                + self.noise_std**2
            )
            f = batch_mul(self.y - h_x_0, 1.0 / C_yy)
            ls = batch_matmul(jnp.transpose(H_grad_x_0, axes=axes), f).reshape(s.shape)
            gs = s + ls
            return gs

        return guidance_score
