import equinox as eqx
import jax
import jax.numpy as jnp
import jax.random as random
from jaxtyping import Array, Float, PRNGKeyArray, PyTree
from optax import GradientTransformation


class FullyConnectedWithTime(eqx.Module):
    """A simple model with multiple fully connected layers and some fourier features for the time
    variable.
    """

    layers: list[eqx.nn.Linear]

    def __init__(self, in_size: int, key: PRNGKeyArray):
        key1, key2, key3, key4 = jax.random.split(key, 4)
        out_size = in_size

        self.layers = [
            eqx.nn.Linear(in_size + 4, 256, key=key1),
            eqx.nn.Linear(256, 256, key=key2),
            eqx.nn.Linear(256, 256, key=key3),
            eqx.nn.Linear(256, out_size, key=key4),
        ]

    def __call__(self, x: Array, t: Array) -> Float[Array, "2"]:
        t_fourier = jnp.array(
            [t - 0.5, jnp.cos(2 * jnp.pi * t), jnp.sin(2 * jnp.pi * t), -jnp.cos(4 * jnp.pi * t)],
        ).squeeze(-1)

        x = jnp.concatenate([x, t_fourier])

        for layer in self.layers[:-1]:
            x = jax.nn.relu(layer(x))

        x = self.layers[-1](x)

        return x


@jax.jit
@jax.value_and_grad
def loss(model: FullyConnectedWithTime, data: Array, alpha_bar: Array, rng) -> Float[Array, ""]:
    key1, key2 = random.split(rng, 2)

    r_alpha_bar = random.choice(key1, alpha_bar, (data.shape[0], 1))

    noise = random.normal(key2, data.shape)
    noised_data = data * r_alpha_bar**0.5 + noise * (1 - r_alpha_bar) ** 0.5

    output = jax.vmap(model)(noised_data, r_alpha_bar)

    loss = jnp.mean((noise - output) ** 2)

    return loss


def fit(
    model: FullyConnectedWithTime,
    steps: int,
    optimizer: GradientTransformation,
    data: Array,
    alpha_bar: Array,
    rng: PRNGKeyArray,
    print_every: int = 5_000,
) -> FullyConnectedWithTime:
    opt_state = optimizer.init(eqx.filter(model, eqx.is_array))
    losses: list[Float] = []

    @eqx.filter_jit
    def make_step(
        model: FullyConnectedWithTime,
        opt_state: PyTree,
        data: Array,
        alpha_bar: Array,
        step_rng: PRNGKeyArray,
    ):
        loss_value, grads = loss(model, data, alpha_bar, step_rng)
        updates, opt_state = optimizer.update(grads, opt_state, model)
        model = eqx.apply_updates(model, updates)

        return model, opt_state, loss_value

    for step in range(steps):
        step_rng, rng = random.split(rng, 2)
        model, opt_state, train_loss = make_step(
            model,
            opt_state,
            data,
            alpha_bar,
            step_rng,
        )
        losses.append(train_loss)

        if (step % print_every) == 0 or (step == steps - 1):
            mean_loss = jnp.mean(jnp.array(losses))
            print(f"{step=},\t avg_train_loss={mean_loss}")

    return model
