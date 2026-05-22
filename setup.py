from setuptools import setup

setup(
    name='phone_camera',
    version='0.0.0',
    author='Kai Ploeger',
    author_email='mail@kaiploeger.net',
    packages=['phone_camera'],
    license='LICENSE.txt',
    description='Record video from an Android IP Webcam phone via ffmpeg.',
    long_description=open('README.md').read(),
    install_requires=[],
)
