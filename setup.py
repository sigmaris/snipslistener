import sys

from setuptools import setup
from setuptools.command.test import test as TestCommand


class PyTest(TestCommand):
    user_options = [('pytest-args=', 'a', "Arguments to pass to pytest")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = ''

    def run_tests(self):
        import shlex
        #import here, cause outside the eggs aren't loaded
        import pytest
        errno = pytest.main(shlex.split(self.pytest_args))
        sys.exit(errno)

setup(
    name='snipslistener',
    version='0.0.6',
    description='Snips skill MQTT listener helper code',
    author='sigmaris@gmail.com',
    url='https://github.com/sigmaris/snipslistener',
    download_url='',
    license='MIT',
    install_requires=['paho-mqtt'],
    tests_require=['pytest'],
    cmdclass = {'test': PyTest},
    keywords=['snips', 'mqtt'],
    py_modules=['snipslistener'],
    entry_points={
        'console_scripts': ['fallbackhandler=snipslistener:run_fallback_handler'],
    }
)
