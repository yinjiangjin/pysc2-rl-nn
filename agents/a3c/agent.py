import threading
import itertools

from absl import flags

from pysc2.agents import base_agent
from pysc2.env import available_actions_printer, sc2_env

import tensorflow as tf

from agents.a3c.worker import Worker

FLAGS = flags.FLAGS

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
        assert(m_size == s_size)
        self.m_size = m_size
        self.s_size = s_size

        self.network_name = network_name

        self.workers = []

        self.saver = None


    def _make_env(self):
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

    def initialize(self, device, worker_count):
        with tf.device("/cpu:0"):
            # Keeps track of the number of updates we've performed
            global_step = tf.Variable(0, name="global_step", trainable=False)

            # Global policy and value nets
            with tf.variable_scope("global") as vs:
                policy_net = None
                value_net = None

            # Global step iterator
            global_counter = itertools.count()

            # Create worker graphs
            for worker_id in range(worker_count):
                worker_summary_writer = None
                if worker_id == 0:
                    worker_summary_writer = self.summary_writer

                worker = Worker(reuse=worker_id > 0,
                                name=self.name,
                                env_fn=self._make_env,
                                device=device[worker_id%len(device)],
                                session=self.session,
                                is_training=self.is_training,
                                m_size=self.m_size,
                                s_size=self.s_size,
                                policy_net=policy_net,
                                value_net=value_net,
                                core_net=self.network_name,
                                global_counter=global_counter,
                                discount_factor=FLAGS.discount,
                                summary_writer=worker_summary_writer,
                                max_global_steps=FLAGS.max_global_steps,
                                map_name=self.map_name)
                self.workers.append(worker)

            self.session.run(tf.global_variables_initializer())
            self.saver = tf.train.Saver(keep_checkpoint_every_n_hours=0.16, max_to_keep=3)

    def run(self):
        with self.session as session:
            session.run(tf.global_variables_initializer())
            coord = tf.train.Coordinator()

            # Load previous checkpoint here
            # ...

            # Start worker threads
            threads = []
            for worker in self.workers:
                worker_fn = lambda worker=worker: worker.run(coord, FLAGS.max_t)
                t = threading.Thread(target=worker_fn)
                threads.append(t)
                t.daemon = True
                t.start()

            coord.join(threads)