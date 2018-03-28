import threading
import itertools

from absl import flags

from pysc2.agents import base_agent
from pysc2.env import available_actions_printer, sc2_env

import tensorflow as tf

from agents.a3c.worker import Worker
from agents.a3c.estimators import PolicyEstimator, ValueEstimator, Optimizer

import util
import time

from runner import Runner

FLAGS = flags.FLAGS


def make_env():
    with sc2_env.SC2Env(
            map_name=FLAGS.map,
            agent_race=FLAGS.agent_race,
            bot_race=FLAGS.bot_race,
            difficulty=FLAGS.difficulty,
            step_mul=FLAGS.step_mul,
            screen_size_px=(FLAGS.screen_resolution, FLAGS.screen_resolution),
            minimap_size_px=(FLAGS.minimap_resolution, FLAGS.minimap_resolution),
            save_replay_episodes=FLAGS.save_replay_frequency,
            replay_dir=FLAGS.save_dir,
            visualize=FLAGS.render) as env:
        return available_actions_printer.AvailableActionsPrinter(env)


class A3CAgent(base_agent.BaseAgent):
    def __init__(self, is_training, m_size, s_size, network_name, map_name, session, summary_writer, name="a3c_agent"):
        super().__init__()
        self.name = name
        self.is_training = is_training
        self.summary = []

        self.session = session
        self.summary_writer = summary_writer
        self.map_name = map_name

        # Dimensions
        # Make sure that screen resolution is equal to minimap resolution
        assert (m_size == s_size)
        self.m_size = m_size
        self.s_size = s_size

        self.network_name = network_name

        self.workers = []
        self.runners = []

        self.saver = None

    def initialize(self, device, worker_count):
        with tf.device(device[0]):
            # Keeps track of the number of updates we've performed
            global_step = tf.Variable(0, name="global_step", trainable=False)

            # Global network
            with tf.variable_scope("global"):
                network = util.init_network(self.network_name, self.m_size, self.s_size)
                state, fc = network.build(util.init_feature_placeholders(self.m_size, self.s_size))
                policy_net = PolicyEstimator(state, fc)
                value_net = ValueEstimator(fc)
                if FLAGS.dual_msprop:
                    optimizers = [
                        Optimizer("global_policy", 1e-3, policy_net.loss),
                        Optimizer("global_value", 1e-3, value_net.loss)
                    ]
                else:
                    optimizers = [
                        Optimizer("global", 1e-3, policy_net.loss + value_net.loss)
                    ]

            # Global iterators
            global_step_counter = itertools.count()
            global_episode_counter = itertools.count()

            # Create worker graphs
            for worker_id in range(worker_count):
                worker_summary_writer = None
                if worker_id == 0:
                    worker_summary_writer = self.summary_writer

                worker = Worker(
                    name="{}_{}".format(self.name, worker_id),
                    device=device[(worker_id + 1) % len(device)],
                    session=self.session,
                    m_size=self.m_size,
                    s_size=self.s_size,
                    global_optimizers=optimizers,
                    network=network,
                    map_name=self.map_name,
                    discount_factor=FLAGS.discount,
                    summary_writer=worker_summary_writer
                )
                self.workers.append(worker)

                runner = Runner(
                    agent=worker,
                    env=make_env(),
                    is_training=self.is_training,
                    global_step_counter=global_step_counter,
                    global_episode_counter=global_episode_counter,
                    update_period=FLAGS.max_update_steps,
                    max_local_steps=FLAGS.max_steps,
                    max_global_steps=FLAGS.max_global_steps,
                    max_global_episodes=FLAGS.num_episodes
                )
                self.runners.append(runner)

            # self.saver = tf.train.Saver(keep_checkpoint_every_n_hours=0.16, max_to_keep=3)

    def run(self):
        with self.session as session:
            session.run(tf.global_variables_initializer())
            coord = tf.train.Coordinator()

            # Load previous checkpoint here
            # ...

            # Start worker threads
            threads = []
            for runner in self.runners:
                t = threading.Thread(
                    target=lambda w_fn=runner: runner.run(coord)
                )
                threads.append(t)
                t.daemon = True
                t.start()
                time.sleep(FLAGS.stagger)

            coord.join(threads)