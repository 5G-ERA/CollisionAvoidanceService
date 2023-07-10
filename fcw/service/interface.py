import base64
import argparse
import binascii
import os
import numpy as np
import time
import socketio
from dataclasses import dataclass
from queue import Queue
from typing import Dict
import threading
import cv2
from flask import Flask
import logging
import sys

from fcw.service.collision_worker import CollisionWorker

from era_5g_interface.task_handler import TaskHandler
from era_5g_interface.task_handler_internal_q import TaskHandlerInternalQ
from era_5g_interface.h264_decoder import H264Decoder
import era_5g_interface.interface_helpers
from era_5g_interface.interface_helpers import HeartBeatSender
from era_5g_interface.dataclasses.control_command import ControlCommand, ControlCmdType

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger("FCW interface")

# port of the netapp's server
NETAPP_PORT = os.getenv("NETAPP_PORT", 5896)
# input queue size
NETAPP_INPUT_QUEUE = int(os.getenv("NETAPP_INPUT_QUEUE", 1))

# the max_http_buffer_size parameter defines the max size of the message to be passed
sio = socketio.Server(async_mode='threading', async_handlers=False, max_http_buffer_size=5 * (1024 ** 2))
app = Flask(__name__)
app.wsgi_app = socketio.WSGIApp(sio, app.wsgi_app)


@dataclass
class TaskAndWorker:
    task: TaskHandler
    worker: CollisionWorker


# list of registered tasks
tasks: Dict[str, TaskAndWorker] = dict()

last_timestamp = 0

heart_beat_sender = HeartBeatSender()


def heart_beat_timer():
    latencies = []
    for task_and_worker in tasks.values():
        latencies.extend(task_and_worker.worker.latency_measurements.get_latencies())
    avg_latency = 0
    if len(latencies) > 0:
        avg_latency = float(np.mean(np.array(latencies)))

    queue_size = 1
    queue_occupancy = 1

    heart_beat_sender.send_middleware_heart_beat(
        avg_latency=avg_latency, queue_size=queue_size, queue_occupancy=queue_occupancy, current_robot_count=len(tasks)
    )
    threading.Timer(era_5g_interface.interface_helpers.MIDDLEWARE_REPORT_INTERVAL, heart_beat_timer).start()


heart_beat_timer()


def get_sid_of_namespace(eio_sid, namespace):
    return sio.manager.sid_from_eio_sid(eio_sid, namespace)


def get_results_sid(eio_sid):
    return sio.manager.sid_from_eio_sid(eio_sid, "/results")


@sio.on('connect', namespace='/data')
def connect_data(sid, environ):
    """Creates a websocket connection to the client for passing the data.

    Raises:
        ConnectionRefusedError: Raised when attempt for connection were made
            without registering first.
    """

    logger.info(f"Connected data. Session id: {sio.manager.eio_sid_from_sid(sid, '/data')}, namespace_id: {sid}")
    sio.send("You are connected", namespace='/data', to=sid)


@sio.on('connect', namespace='/control')
def connect_control(sid, environ):
    """_summary_
    Creates a websocket connection to the client for passing control commands.

    Raises:
        ConnectionRefusedError: Raised when attempt for connection were made
            without registering first.
    """

    logger.info(f"Connected control. Session id: {sio.manager.eio_sid_from_sid(sid, '/control')}, namespace_id: {sid}")
    sio.send("You are connected", namespace='/control', to=sid)


@sio.on('connect', namespace='/results')
def connect_results(sid, environ):
    """Creates a websocket connection to the client for passing the results.

    Raises:
        ConnectionRefusedError: Raised when attempt for connection were made
            without registering first.
    """

    logger.info(f"Connected results. Session id: {sio.manager.eio_sid_from_sid(sid, '/results')}, namespace_id: {sid}")
    sio.send("You are connected", namespace='/results', to=sid)


@sio.on('image', namespace='/data')
def image_callback_websocket(sid, data: dict):
    """Allows to receive jpg or h264 encoded image using the websocket transport

    Args:
        sid ():
        data (dict): A base64 encoded image frame and (optionally) related timestamp in format:
            {'frame': 'base64data', 'timestamp': 'int'}

    Raises:
        ConnectionRefusedError: Raised when attempt for connection were made
            without registering first or frame was not passed in correct format.
    """

    if 'timestamp' in data:
        timestamp = data['timestamp']
    else:
        logger.info("Timestamp not set, setting default value")
        timestamp = 0

    eio_sid = sio.manager.eio_sid_from_sid(sid, "/data")

    if eio_sid not in tasks:
        logger.error(f"Non-registered client tried to send data")
        sio.emit(
            "image_error",
            {"timestamp": timestamp,
             "error": "Not connected"},
            namespace='/data',
            to=sid
        )
        return

    if 'frame' not in data:
        logger.error(f"Data does not contain frame.")
        sio.emit(
            "image_error",
            {"timestamp": timestamp,
             "error": "Data does not contain frame."},
            namespace='/data',
            to=sid
        )
        return

    global last_timestamp
    if timestamp - last_timestamp < 0:
        logger.error(
            f"Received frame with older timestamp: {timestamp}, "
            f"last_timestamp: {last_timestamp}, diff: {timestamp - last_timestamp}"
        )
        sio.emit(
            "image_error",
            {"timestamp": timestamp,
             "error": f"Received frame with older timestamp: {timestamp}, "
                      f"last_timestamp: {last_timestamp}, diff: {timestamp - last_timestamp}"},
            namespace='/data',
            to=sid
        )
        return

    last_timestamp = timestamp

    task = tasks[eio_sid].task
    try:
        if task.decoder:
            image = task.decoder.decode_packet_data(data["frame"])
        else:
            frame = base64.b64decode(data["frame"])
            image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    except (ValueError, binascii.Error, Exception) as error:
        logger.error(f"Failed to decode frame data: {error}")
        sio.emit(
            "image_error",
            {"timestamp": timestamp,
             "error": f"Failed to decode frame data: {error}"},
            namespace='/data',
            to=sid
        )
        return

    task.store_image(
        {"sid": eio_sid,
         "timestamp": timestamp,
         "recv_timestamp": time.perf_counter_ns(),
         "websocket_id": get_results_sid(sio.manager.eio_sid_from_sid(sid, "/data"))},
        image
    )


@sio.on('json', namespace='/data')
def json_callback_websocket(sid, data):
    """
    Allows to receive general json data using the websocket transport

    Args:
        data (dict): NetApp-specific json data

    Raises:
        ConnectionRefusedError: Raised when attempt for connection were made
            without registering first.
    """
    print(data)

    logger.info(f"Client with task id: {sio.manager.eio_sid_from_sid(sid, '/data')} sent data {data}")


@sio.on('command', namespace='/control')
def command_callback_websocket(sid, data: Dict):
    command = ControlCommand(**data)
    logger.info(f"Control command {command} processing: session id: {sid}")
    if command and command.cmd_type == ControlCmdType.SET_STATE:
        args = command.data
        h264 = False
        config = {}
        camera_config = {}
        fps = 30
        width = 0
        height = 0
        if args:
            h264 = args.get("h264", False)
            config = args.get("config", {})
            camera_config = args.get("camera_config", {})
            fps = args.get("fps", 30)
            width = args.get("width", 0)
            height = args.get("height", 0)
            logger.info(f"H264: {h264}")
            logger.info(f"Config: {config}")
            logger.info(f"Camera config: {camera_config}")
            logger.info(f"Video {width}x{height}, {fps} FPS")

        # queue with received images
        image_queue = Queue(NETAPP_INPUT_QUEUE)
        eio_sid = sio.manager.eio_sid_from_sid(sid, '/control')

        if h264:
            task = TaskHandlerInternalQ(eio_sid, image_queue, decoder=H264Decoder(fps, width, height))
        else:
            task = TaskHandlerInternalQ(eio_sid, image_queue)

        # Create worker
        worker = CollisionWorker(
            image_queue, sio,
            config, camera_config, fps,
            name=f"Detector {eio_sid}"
        )

        tasks[eio_sid] = TaskAndWorker(task, worker)
        tasks[eio_sid].worker.start()
        t0 = time.perf_counter_ns()
        while True:
            if tasks[eio_sid].worker.is_alive():
                break
            if time.perf_counter_ns() > t0 + 5 * 1.0e+9:
                logger.error(f"Timed out to start worker. Session id: {eio_sid}, namespace_id: {sid}")
                raise ConnectionRefusedError('Timed out to start worker.')

        logger.info(f"Task handler and worker created and started: {eio_sid}")

    logger.info(f"Control command {command} applied: session id: {sid}")


@sio.on('disconnect', namespace='/data')
def disconnect_data(sid):
    eio_sid = sio.manager.eio_sid_from_sid(sid, "/data")
    task_and_worker = tasks.get(eio_sid)
    if task_and_worker:
        task_and_worker.worker.stop()
        task_and_worker.worker.join()
        del tasks[eio_sid]
        del task_and_worker
        logger.info(f"Task handler and worker deleted: {eio_sid}")

    logger.info(f"Client disconnected from /data namespace: session id: {sid}")


@sio.on('disconnect', namespace='/control')
def disconnect_control(sid):
    logger.info(f"Client disconnected from /control namespace: session id: {sid}")


@sio.on('disconnect', namespace='/results')
def disconnect_results(sid):
    logger.info(f"Client disconnected from /results namespace: session id: {sid}")


def main(args=None):
    parser = argparse.ArgumentParser(description='Standalone variant of Forward Collision Warning NetApp')
    args = parser.parse_args()

    logger.info(f"The size of the queue set to: {NETAPP_INPUT_QUEUE}")

    # runs the flask server
    # allow_unsafe_werkzeug needs to be true to run inside the docker
    # TODO: use better webserver
    app.run(port=NETAPP_PORT, host='0.0.0.0')


if __name__ == '__main__':
    main()
