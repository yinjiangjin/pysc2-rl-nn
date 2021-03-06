import tensorflow as tf
import numpy as np

from pysc2.agents.base_agent import BaseAgent
from pysc2.lib import actions, stopwatch

from common import util

from agents.a3c.estimators import configure_estimators

sw = stopwatch.StopWatch()


class Worker(BaseAgent):
    def __init__(self,
                 name,
                 device,
                 session,
                 m_size,
                 s_size,
                 global_optimizers,
                 network,
                 map_name,
                 learning_rate,
                 discount_factor,
                 eta,
                 beta,
                 summary_writer=None):

        super().__init__()
        self.name = name
        self.discount_factor = discount_factor
        self.learning_rate = learning_rate
        self.eta = eta
        self.beta = beta
        self.global_step = tf.train.get_global_step()

        self.device = device
        self.session = session

        self.summary = []

        self.summary_writer = summary_writer
        self.map_name = map_name

        # Dimensions
        self.m_size = m_size
        self.s_size = s_size

        # Network
        self.dual_msprop = len(global_optimizers) > 1
        if self.dual_msprop:
            self.global_policy_optimizer, self.global_value_optimizer = global_optimizers
        else:
            self.global_optimizer = global_optimizers[0]

        # Tensor dictionaries
        self.valid_actions = {}

        # Saver
        self.saver = None

        # Placeholders
        self.features = util.init_feature_placeholders(m_size, s_size)

        # build the local model
        self._build_model(network, device)

    def _build_model(self, network, device):
        with tf.device(device):
            with tf.variable_scope(self.name):
                self.policy_net, self.value_net, self.optimizer = configure_estimators(
                    network,
                    self.features,
                    self.eta,
                    self.beta,
                    self.learning_rate,
                    self.dual_msprop,
                    self.summary_writer
                )

                # Create op: Copy global variables
                self.copy_params_op = util.make_copy_params_op(
                    tf.contrib.slim.get_variables(scope="global", collection=tf.GraphKeys.TRAINABLE_VARIABLES),
                    tf.contrib.slim.get_variables(scope=self.name+'/', collection=tf.GraphKeys.TRAINABLE_VARIABLES)
                )

                # Create op: Update global variables with local losses
                if self.dual_msprop:
                    self.policy_optimizer = self.optimizer[0]
                    self.value_optimizer = self.optimizer[1]
                    self.pnet_train_op = util.make_train_op(self.policy_optimizer, self.global_policy_optimizer)
                    self.vnet_train_op = util.make_train_op(self.value_optimizer, self.global_value_optimizer)
                else:
                    self.optimizer = self.optimizer[0]
                    self.single_train_op = util.make_train_op(self.optimizer, self.global_optimizer)

    def _policy_net_predict(self, obs):
        feed_dict = {
             self.features["minimap"]: util.minimap_obs(obs),
             self.features["screen"]: util.screen_obs(obs),
             self.features["info"]: util.non_spatial_obs(obs, self.s_size)
        }

        # Get spatial/non_spatial policies
        policy = self.session.run(
            {
                "spatial": self.policy_net.prediction["spatial"],
                "non_spatial": self.policy_net.prediction["non_spatial"],
            },
            feed_dict=feed_dict
        )

        policy["spatial"] = policy["spatial"].ravel()
        policy["non_spatial"] = policy["non_spatial"].ravel()

        return policy

    def _value_net_predict(self, obs):
        feed_dict = {
             self.features["minimap"]: util.minimap_obs(obs),
             self.features["screen"]: util.screen_obs(obs),
             self.features["info"]: util.non_spatial_obs(obs, self.s_size)
        }

        return self.session.run(self.value_net.prediction, feed_dict=feed_dict)

    def reset(self):
        self.episodes += 1
        self.steps = 0

    def _exploit_random(self, valid_actions):
        # Choose a random valid action
        act_id = np.random.choice(valid_actions)

        # Choose random target
        target = [np.random.randint(0, self.s_size),
                  np.random.randint(0, self.s_size)]

        return act_id, target

    def _exploit_max(self, policy, valid_actions):
        # Choose 'best' valid action
        act_id = valid_actions[np.argmax(policy["non_spatial"][valid_actions])]
        target = np.argmax(policy["spatial"])

        # Resize to provided resolution
        # Example:
        #   target = 535 -> 535 // 64 = 8, 535 % 64 = 24
        #   target = [8, 24]
        target = [int(target // self.s_size), int(target % self.s_size)]

        return act_id, target

    def _exploit_distribution(self, policy, valid_actions):
        # Mask actions
        non_spatial_policy = policy["non_spatial"][valid_actions]

        # Normalize probabilities
        non_spatial_probs = non_spatial_policy/np.sum(non_spatial_policy)

        # Choose from normalized distribution
        act_id = np.random.choice(valid_actions, p=non_spatial_probs)
        target = np.random.choice(np.arange(len(policy["spatial"])), p=policy["spatial"])

        # Resize to provided resolution
        coords = [int(target // self.s_size), int(target % self.s_size)]

        return act_id, coords

    def step(self, obs):
        policy = self._policy_net_predict(obs)
        valid_actions = obs.observation['available_actions']

        # e-greedy
        if np.random.random() < 0.3:
            # act_id, target = self._exploit_random(valid_actions)
            act_id, target = self._exploit_distribution(policy, valid_actions)
        else:
            act_id, target = self._exploit_max(policy, valid_actions)

        # policy-dependent, encourage exploration with entropy regularization
        # act_id, target = self._exploit_distribution(policy, valid_actions)
        act_args = util.get_action_arguments(act_id, target)

        self.steps += 1

        return actions.FunctionCall(act_id, act_args)

    def update(self, replay_buffer):
        # Compute value of last observation
        obs = replay_buffer[-1][-1]

        # R = 0 for terminal s_t, V(s_t, θ'_v) for non-terminal s_t
        # i.e. bootstrap from last state
        reward = 0.0
        if not obs.last():
            reward = self._value_net_predict(obs)

        # Preallocate array sizes for _*speed*_
        value_targets = np.zeros([len(replay_buffer)], dtype=np.float32)
        advantages = np.zeros([len(replay_buffer)], dtype=np.float32)
        value_targets[-1] = reward

        valid_spatial_actions = np.zeros([len(replay_buffer)], dtype=np.float32)
        spatial_actions_selected = np.zeros([len(replay_buffer), self.s_size ** 2], dtype=np.float32)
        valid_non_spatial_actions = np.zeros([len(replay_buffer), len(actions.FUNCTIONS)], dtype=np.float32)
        non_spatial_actions_selected = np.zeros([len(replay_buffer), len(actions.FUNCTIONS)], dtype=np.float32)

        minimaps = []
        screens = []
        infos = []

        # Accumulate batch updates
        replay_buffer.reverse()
        for i, [action, obs] in enumerate(replay_buffer):
            # Update state
            minimaps.append(util.minimap_obs(obs))
            screens.append(util.screen_obs(obs))
            infos.append(util.non_spatial_obs(obs, self.s_size))

            # Update reward
            # R <- r_i + γR
            reward = int(obs.observation["score_cumulative"][0]) + self.discount_factor * reward

            # advantage = R - V(s_i; θ'_v)
            advantage = (reward - self._value_net_predict(obs))

            advantages[i] = advantage           # Append advantage
            value_targets[i] = reward           # Append discounted reward

            # Get selected action
            act_id = action.function
            act_args = action.arguments

            # Append selected action
            valid_actions = obs.observation["available_actions"]
            valid_non_spatial_actions[i, valid_actions] = 1
            non_spatial_actions_selected[i, act_id] = 1

            args = actions.FUNCTIONS[act_id].args
            for arg, act_arg in zip(args, act_args):
                if arg.name in ('screen', 'minimap', 'screen2'):
                    ind = act_arg[1] * self.s_size + act_arg[0]
                    valid_spatial_actions[i] = 1
                    spatial_actions_selected[i, ind] = 1

        minimaps = np.concatenate(minimaps, axis=0)
        screens = np.concatenate(screens, axis=0)
        infos = np.concatenate(infos, axis=0)

        # Train
        feed_dict = {
            self.features["minimap"]: minimaps,
            self.features["screen"]: screens,
            self.features["info"]: infos,
            self.policy_net.valid["spatial"]: valid_spatial_actions,
            self.policy_net.valid["non_spatial"]: valid_non_spatial_actions,
            self.policy_net.actions["spatial"]: spatial_actions_selected,
            self.policy_net.actions["non_spatial"]: non_spatial_actions_selected,
            self.policy_net.advantages: advantages,
            self.value_net.targets: value_targets,
        }

        if self.dual_msprop:
            fetch = [
                self.global_step,
                self.policy_net.loss,
                self.value_net.loss,
                self.pnet_train_op,
                self.vnet_train_op
            ]
        else:
            fetch = [
                self.global_step,
                self.policy_net.loss,
                self.value_net.loss,
                self.single_train_op,
            ]

        if self.summary_writer is None:
            global_step, pnet_loss, vnet_loss = self.session.run(
                fetch,
                feed_dict
            )[:3]
        else:
            fetch.append(self.policy_net.summaries)
            fetch.append(self.value_net.summaries)

            if self.dual_msprop:
                global_step, pnet_loss, vnet_loss, _, _, pnet_summaries, vnet_summaries = self.session.run(
                    fetch,
                    feed_dict
                )
            else:
                global_step, pnet_loss, vnet_loss, _, pnet_summaries, vnet_summaries = self.session.run(
                    fetch,
                    feed_dict
                )

            # Write summaries
            self.summary_writer.add_graph(self.session.graph)
            self.summary_writer.add_summary(pnet_summaries, global_step)
            self.summary_writer.add_summary(vnet_summaries, global_step)

            self.summary_writer.flush()

        return pnet_loss, vnet_loss
