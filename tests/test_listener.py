import json
import logging
from unittest import mock

import pytest
import paho.mqtt.client

from snipslistener import intent, SnipsListener, IntentDetected, SessionEnded

LOG = logging.getLogger(__name__)


class ExampleSkill(SnipsListener):

    def __init__(self):
        super().__init__('test-mqtt-example')

    @intent('multiturn')
    def multi_turn_gen(self, data):
        LOG.info("Starting multi-turn dialogue")

        result = yield "Reply to user 1"
        if isinstance(result, IntentDetected):
            LOG.info("User replied with %r", result)
        elif isinstance(result, SessionEnded):
            LOG.info("Session ended with %r", result)
        else:
            raise ValueError(result)

        result = yield ("Reply to user 2", ['intent_filter_1', "intent_filter_2"])
        if isinstance(result, IntentDetected):
            LOG.info("User replied with %r", result)
        elif isinstance(result, SessionEnded):
            LOG.info("Session ended with %r", result)
        else:
            raise ValueError(result)

        return "final text to user"

    @intent('singleturn')
    def single_turn(self, data):
        LOG.info("Starting single-turn dialogue")
        data.session_manager.end_session("Single and final reply to user")


class ExampleMessage(object):

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode('utf-8')


@pytest.fixture
def skill():
    return ExampleSkill()


@pytest.fixture
def mqtt_client():
    return mock.create_autospec(paho.mqtt.client.Client())


@pytest.fixture
def multiturn_intent():
    return ExampleMessage(
        "hermes/intent/multiturn", json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'siteId': 'default',
            'input': 'foo bar baz',
            'intent': {
                'intentName': 'multiturn',
                'probability': 0.723,
            },
            'slots': []
        })
    )


@pytest.fixture
def singleturn_intent():
    return ExampleMessage(
        "hermes/intent/singleturn", json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'siteId': 'default',
            'input': 'foo bar baz',
            'intent': {
                'intentName': 'singleturn',
                'probability': 0.723,
            },
            'slots': []
        })
    )



def test_multiturn_generator(skill, mqtt_client, multiturn_intent):
    skill._handle_intent(mqtt_client, None, multiturn_intent)
    mqtt_client.publish.assert_called_with(
        'hermes/dialogueManager/continueSession',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'text': "Reply to user 1",
        })
    )
    assert len(skill._suspended_sessions) == 1
    assert len(skill._session_managers) == 1
    skill._handle_intent(mqtt_client, None, multiturn_intent)
    mqtt_client.publish.assert_called_with(
        'hermes/dialogueManager/continueSession',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'text': "Reply to user 2",
            'intentFilter': ['intent_filter_1', "intent_filter_2"],
        })
    )
    assert len(skill._suspended_sessions) == 1
    assert len(skill._session_managers) == 1
    skill._handle_intent(mqtt_client, None, multiturn_intent)
    mqtt_client.publish.assert_called_with(
        'hermes/dialogueManager/endSession',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'text': "final text to user",
        })
    )
    assert len(skill._suspended_sessions) == 0
    assert len(skill._session_managers) == 1
    skill._handle_session_ended(mqtt_client, None, ExampleMessage(
        'hermes/dialogueManager/sessionEnded',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'siteId': 'default',
            'termination': {'reason': 'abortedByUser'}
        })
    ))
    assert len(skill._suspended_sessions) == 0
    assert len(skill._session_managers) == 0


def test_premature_end(skill, mqtt_client, multiturn_intent):
    skill._handle_intent(mqtt_client, None, multiturn_intent)
    mqtt_client.publish.assert_called_with(
        'hermes/dialogueManager/continueSession',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'text': "Reply to user 1",
        })
    )
    assert len(skill._suspended_sessions) == 1
    assert len(skill._session_managers) == 1
    skill._handle_session_ended(mqtt_client, None, ExampleMessage(
        'hermes/dialogueManager/sessionEnded',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'siteId': 'default',
            'termination': {'reason': 'abortedByUser'}
        })
    ))
    assert len(skill._suspended_sessions) == 0
    assert len(skill._session_managers) == 0


def test_single_turn(skill, mqtt_client, singleturn_intent):
    skill._handle_intent(mqtt_client, None, singleturn_intent)
    mqtt_client.publish.assert_called_with(
        'hermes/dialogueManager/endSession',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'text': "Single and final reply to user",
        })
    )
    assert len(skill._suspended_sessions) == 0
    assert len(skill._session_managers) == 1
    skill._handle_session_ended(mqtt_client, None, ExampleMessage(
        'hermes/dialogueManager/sessionEnded',
        payload=json.dumps({
            'sessionId': 'aaaa-bbbb-cccc',
            'siteId': 'default',
            'termination': {'reason': 'nominal'}
        })
    ))
    assert len(skill._suspended_sessions) == 0
    assert len(skill._session_managers) == 0
