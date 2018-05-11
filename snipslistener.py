import argparse
import collections
import inspect
import json
import logging
import logging.config

import paho.mqtt.client as mqtt

LOG = logging.getLogger(__name__)


def intent(name, namespace=None):
    def decorate(func):
        handles = getattr(func, '_handles_intent', [])
        handles.append((name, namespace))
        func._handles_intent = handles
        return func
    return decorate


def hotword_detected(func=None):
    def decorate(f):
        f._handles_hotword_detected = True
        return f
    if func is not None:
        return decorate(func)
    else:
        return decorate


def session_ended(func=None):
    def decorate(f):
        f._handles_session_ended = True
        return f
    if func is not None:
        return decorate(func)
    else:
        return decorate


class SessionManager(object):

    def __init__(self, session_id, site_id, mqtt):
        self.session_id = session_id
        self.site_id = site_id
        self.mqtt = mqtt
        self.ended = False

    def continue_session(self, text, intent_filters=None):
        if self.ended:
            LOG.error("Trying to continue an already-ended session %s", self.session_id)
            return

        payload = {
            'sessionId': self.session_id,
            'text': text
        }
        if intent_filters:
            payload['intentFilter'] = intent_filters
        self.mqtt.publish(
            'hermes/dialogueManager/continueSession',
            payload=json.dumps(payload)
        )

    def say(self, text):
        if self.ended:
            LOG.error("Trying to say something an already-ended session %s", self.session_id)
            return

        payload = {
            'sessionId': self.session_id,
            'siteId': self.site_id,
            'text': text
        }
        self.mqtt.publish(
            'hermes/tts/say',
            payload=json.dumps(payload)
        )

    def end_session(self, text=None):
        if self.ended:
            LOG.error("Trying to end an already-ended session %s", self.session_id)
            return

        payload = {
            'sessionId': self.session_id,
        }
        if text:
            payload['text'] = text
        self.mqtt.publish(
            'hermes/dialogueManager/endSession',
            payload=json.dumps(payload)
        )
        self.ended = True


HotwordDetected = collections.namedtuple('HotwordDetected', ('hotword_id', 'model_id', 'site_id'))
IntentDetected = collections.namedtuple('IntentDetected', (
    'session_id', 'site_id', 'custom_data', 'input', 'intent_name', 'probability', 'slots', 'session_manager'
))
Slot = collections.namedtuple('Slot', ('slot_name', 'raw_value', 'value', 'value_kind', 'range', 'entity', 'text'))
Range = collections.namedtuple('Range', ('start', 'end'))
SessionEnded = collections.namedtuple('SessionEnded', ('session_id', 'site_id', 'custom_data', 'reason', 'error'))
ContinueSession = collections.namedtuple('ContinueSession', ('text', 'intent_filters'))


class SnipsListener(object):

    def __init__(self, mqtt_host, mqtt_port=1883):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self._mqtt_client = None
        self._intent_handlers = collections.defaultdict(list)
        self._hotword_detected_handlers = set()
        self._session_ended_handlers = set()
        self._suspended_sessions = {}
        self._session_managers = {}
        for attrname in dir(self):
            if attrname[:2] != '__':
                attr = getattr(self, attrname)
                if callable(attr):
                    if hasattr(attr, '_handles_intent'):
                        for intent_desc in attr._handles_intent:
                            self._intent_handlers[intent_desc].append(attr)
                    if getattr(attr, '_handles_hotword_detected', False):
                        self._hotword_detected_handlers.add(attr)
                    if getattr(attr, '_handles_session_ended', False):
                        self._session_ended_handlers.add(attr)

    # The callback for when the client receives a CONNACK response from the server.
    def on_connect(self, client, userdata, flags, rc):
        LOG.debug("Connected to MQTT with result code %s", rc)

        # Subscribing in on_connect() means that if we lose the connection and
        # reconnect then subscriptions will be renewed.
        # TODO: only subscribe to recognized intents?
        client.subscribe("hermes/intent/#")
        client.subscribe("hermes/hotword/+/detected")
        client.subscribe("hermes/dialogueManager/sessionEnded")
        client.subscribe("hermes/nlu/#")
        client.subscribe("hermes/asr/#")
        # client.subscribe("hermes/dialogueManager/#")

    def asr(self, client, userdata, msg):
        LOG.debug("ASR debug: %s -> %s", msg.topic, msg.payload.decode())

    def nlu(self, client, userdata, msg):
        LOG.debug("NLU debug: %s -> %s", msg.topic, msg.payload.decode())

    # def dialogueManager(self, client, userdata, msg):
    #     data = json.loads(msg.payload.decode())
    #     print(data)
    #     print(msg.topic+" "+str(msg.payload.decode()))

    def _register_session_end_handler(self, handler):
        self._session_ended_handlers.add(handler)

    def _unregister_session_end_handler(self, handler):
        self._session_ended_handlers.remove(handler)

    # The callback for when a PUBLISH message is received from the server.
    def _handle_intent(self, client, userdata, msg):
        LOG.debug(msg.topic+" "+str(msg.payload.decode()))
        data = json.loads(msg.payload.decode())
        intent_data = data['intent']
        session_id = data['sessionId']
        site_id = data['siteId']
        LOG.debug("data.sessionId="+str(session_id))
        LOG.debug("data.intent="+str(intent_data))
        LOG.debug("data.slots="+str(data['slots']))

        gen_obj = None
        handlers = None
        if session_id in self._suspended_sessions:
            # Resuming a suspended session
            gen_obj = self._suspended_sessions[session_id]
            LOG.debug("Resuming suspended session %s", session_id)
        else:
            # New session
            split_name = intent_data['intentName'].split(':', 1)
            if len(split_name) == 1:
                # no namespace
                lookup = (split_name[0], None)
            else:
                # name with namespace
                lookup = (split_name[1], split_name[0])

            LOG.debug("Looking for {} in {}".format(lookup, self._intent_handlers.keys()))
            handlers = self._intent_handlers.get(lookup)
            if handlers is None and lookup[1] is not None:
                # Try again with no namespace
                handlers = self._intent_handlers.get((lookup[0], None))
            LOG.debug("Lookup result: {}".format(handlers))

        if handlers is not None or gen_obj is not None:
            if session_id in self._session_managers:
                session_manager = self._session_managers[session_id]
            else:
                session_manager = SessionManager(session_id=session_id, site_id=site_id, mqtt=client)
                self._session_managers[session_id] = session_manager
            intent_obj = IntentDetected(
                session_id=session_id,
                site_id=site_id,
                custom_data=data.get('customData'),
                input=data['input'],
                intent_name=intent_data['intentName'],
                probability=intent_data['probability'],
                slots={
                    s['slotName']: Slot(
                        slot_name=s['slotName'],
                        raw_value=s['rawValue'],
                        value=s['value']['value'],
                        value_kind=s['value']['kind'],
                        range=Range(start=s['range']['start'], end=s['range']['end']),
                        entity=s['entity'],
                        text=data['input'][s['range']['start']:s['range']['end']]
                    )
                    for s in data.get('slots', [])
                },
                session_manager=session_manager
            )

            LOG.debug("Intent object: {!r}".format(intent_obj))
            if gen_obj is not None:
                # Resumed session
                LOG.debug("Sending into generator for %s", session_id)
                self._do_generator_turn(gen_obj, intent_obj, session_id)
            else:
                # new session
                for h in handlers.copy():
                    try:
                        if inspect.isgeneratorfunction(h):
                            # Deal with intent handlers as generators
                            LOG.debug("Getting generator from {}".format(h))
                            gen_obj = h(intent_obj)
                            self._do_generator_turn(gen_obj, intent_obj, session_id, is_start=True)
                        else:
                            h(intent_obj)
                    except Exception as exc:
                        LOG.exception("Exception in %s: %s", h, exc)

    def _do_generator_turn(self, gen_obj, intent_obj, session_id, is_start=False):
        try:
            if is_start:
                turn = next(gen_obj)
            else:
                turn = gen_obj.send(intent_obj)
        except StopIteration as exc:
            if session_id in self._suspended_sessions:
                del self._suspended_sessions[session_id]
            if exc.value:
                intent_obj.session_manager.end_session(exc.value)
            else:
                intent_obj.session_manager.end_session()
        else:
            if isinstance(turn, str):
                self._suspended_sessions[session_id] = gen_obj
                intent_obj.session_manager.continue_session(turn)
            elif len(turn) == 2:
                self._suspended_sessions[session_id] = gen_obj
                intent_obj.session_manager.continue_session(text=turn[0], intent_filters=turn[1])
            else:
                gen_obj.throw(TypeError(
                    "Intent handler generators must yield text or (text: str, intent_filters: list)"
                ))

    def _handle_hotword_detected(self, client, userdata, msg):
        topic = msg.topic
        LOG.debug(topic+" "+str(msg.payload.decode()))
        _, _, hotword_id, _ = topic.split('/')
        data = json.loads(msg.payload.decode())
        for h in self._hotword_detected_handlers.copy():
            try:
                h(HotwordDetected(hotword_id, data['modelId'], data['siteId']))
            except Exception as exc:
                LOG.exception("Exception in %s: %s", h, exc)

    def _handle_session_ended(self, client, userdata, msg):
        topic = msg.topic
        LOG.debug(topic+" "+str(msg.payload.decode()))
        data = json.loads(msg.payload.decode())
        termination = data['termination']
        session_id = data['sessionId']
        ended_msg = SessionEnded(
            session_id, data['siteId'], data.get('customData'),
            termination['reason'], termination.get('error')
        )
        if session_id in self._suspended_sessions:
            # Ending a suspended session
            gen_obj = self._suspended_sessions[session_id]
            try:
                gen_obj.send(ended_msg)
            except Exception as exc:
                LOG.exception("Exception ending suspended session %s: %s", session_id, exc)
            # Remove the suspended session
            del self._suspended_sessions[session_id]
        for h in self._session_ended_handlers.copy():
            try:
                h(ended_msg)
            except Exception as exc:
                LOG.exception("Exception in %s: %s", h, exc)
        if session_id in self._session_managers:
            del self._session_managers[session_id]

    # @intent('convertUnits', 'sigmaris')
    # def demo_intent(self, data):
    #     print("demo intent")
    #     print(repr(data))

        # # We didn't recognize that intent.
        # else:
        #     payload = {
        #         'sessionId': data.get('sessionId', ''),
        #         'text': "I am not sure what to do",
        #     }
        #     publish.single('hermes/dialogueManager/endSession',
        #                    payload=json.dumps(payload),
        #                    hostname=self.mqtt_host,
        #                    port=self.mqtt_port)
#
# def intentNotParsed(client, userdata, msg):
#     print(msg.topic+" "+str(msg.payload.decode()))
#     data = json.loads(msg.payload.decode())
#     print(data)
#
#     # I am actually not sure what message is sent for partial queries
#     if 'sessionId' in data:
#         payload = {'text': 'I am not listening to you anymore',
#                    'sessionId': data.get('sessionId', '')
#                    }
#         publish.single('hermes/dialogueManager/endSession',
#                        payload=json.dumps(payload),
#                        hostname=mqtt_host,
#                        port=mqtt_port)
#
# def intentNotRecognized(client, userdata, msg):
#     print(msg.topic+" "+str(msg.payload.decode()))
#     data = json.loads(msg.payload.decode())
#     print(data)
#
#     # Intent isn't recognized so session will already have ended
#     # so we send a notification instead.
#     if 'sessionId' in data:
#         payload = {'siteId': data.get('siteId', ''),
#                    'init': {'type': 'notification',
#                             'text': "I didn't understand you"
#                            }
#                    }
#         publish.single('hermes/dialogueManager/startSession',
#                        payload=json.dumps(payload),
#                        hostname=mqtt_host,
#                        port=mqtt_port)
#
#
# # setTimer intent handler, doesn't actually do anything as you can see
# def setTimer(client, userdata, msg):
#     print(msg.topic+" "+str(msg.payload.decode()))
#     data = json.loads(msg.payload.decode())
#     print("data.sessionId"+data['sessionId'])
#     print("data.intent"+data['intent'])
#     print("data.slots"+data['slots'])

    def connect(self):
        self._mqtt_client = mqtt.Client()
        self._mqtt_client.on_connect = self.on_connect

        # These are here just to print random info for you
        self._mqtt_client.message_callback_add("hermes/asr/#", self.asr)
        # client.message_callback_add("hermes/dialogueManager/#", self.dialogueManager)
        self._mqtt_client.message_callback_add("hermes/nlu/#", self.nlu)
        # client.message_callback_add("hermes/nlu/intentNotParsed", self.intentNotParsed)
        # client.message_callback_add("hermes/nlu/intentNotRecognized",
        #                             self.intentNotRecognized)

        # This function responds to all intents
        # TODO: intent namespacing? maybe that goes in subscription code
        self._mqtt_client.message_callback_add("hermes/intent/#", self._handle_intent)
        self._mqtt_client.message_callback_add("hermes/hotword/+/detected", self._handle_hotword_detected)
        self._mqtt_client.message_callback_add("hermes/dialogueManager/sessionEnded", self._handle_session_ended)

        self._mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)

    def loop_forever(self):
        if self._mqtt_client is None:
            self.connect()

        # Blocking call that processes network traffic, dispatches callbacks and
        # handles reconnecting.
        # Other loop*() functions are available that give a threaded interface and a
        # manual interface.
        self._mqtt_client.loop_forever()


class FallbackHandler(SnipsListener):

    @session_ended
    def explain_unrecognised(self, data):
        if data.reason == "intentNotRecognized":
            payload = {
                'sessionId': data.session_id,
                'siteId': data.site_id,
                'text': "Sorry, I didn't understand that."
            }
            self._mqtt_client.publish(
                'hermes/tts/say',
                payload=json.dumps(payload)
            )


def run_fallback_handler():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", help="Configuration JSON file", default="config.json")
    args = parser.parse_args()
    with open(args.config, 'r') as infile:
        config = json.load(infile)
        listener_args = {
            "mqtt_host": config["mqtt_host"],
        }
        if 'mqtt_port' in config:
            listener_args['mqtt_port'] = int(config['mqtt_port'])
        if 'logging_config' in config:
            logging.config.dictConfig(config['logging_config'])
        else:
            logging.basicConfig(level=logging.INFO)
        listener = FallbackHandler(**listener_args)
        listener.loop_forever()
