#!/usr/bin/env python3
"""
DVR Feeder — connects to the HiEasy DVR and outputs raw H.264 to stdout.

Designed to be piped to ffmpeg for RTSP publishing to mediamtx:

  DVR_HOST=192.168.1.x python3 dvr_feeder.py --channel 0 | \
    ffmpeg -fflags +genpts -r 25 -f h264 -i pipe:0 -c copy -f rtsp rtsp://localhost:8554/ch0

Or used with mediamtx's runOnDemand to start on first viewer connect.

Environment variables (all overridable via CLI flags):
  DVR_HOST        DVR IP address (required — no default)
  DVR_CMD_PORT    Command port  (default: 5050)
  DVR_MEDIA_PORT  Media port    (default: 6050)
  DVR_USERNAME    Username      (default: admin)
  DVR_PASSWORD    Password      (default: 123456)
"""
import sys
import os
import signal
import argparse
import logging

# Add parent directory to path for the hieasy_dvr package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hieasy_dvr import DVRClient

log = logging.getLogger('dvr_feeder')


def main():
    parser = argparse.ArgumentParser(description='DVR H.264 stream feeder')
    parser.add_argument('-c', '--channel', type=int, default=0,
                        help='Camera channel (0-3, default: 0)')
    parser.add_argument('-s', '--stream-type', type=int, default=1,
                        help='Stream type (1=main, 2=sub, default: 1)')
    parser.add_argument('--host', default=os.environ.get('DVR_HOST'),
                        help='DVR IP address (or set DVR_HOST env var)')
    parser.add_argument('--cmd-port', type=int,
                        default=int(os.environ.get('DVR_CMD_PORT', '5050')))
    parser.add_argument('--media-port', type=int,
                        default=int(os.environ.get('DVR_MEDIA_PORT', '6050')))
    parser.add_argument('--username', default=os.environ.get('DVR_USERNAME', 'admin'))
    parser.add_argument('--password', default=os.environ.get('DVR_PASSWORD', '123456'))
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    if not args.host:
        parser.error('DVR host is required: use --host or set DVR_HOST env var')

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        stream=sys.stderr,
    )

    dvr = DVRClient(
        host=args.host,
        cmd_port=args.cmd_port,
        media_port=args.media_port,
        username=args.username,
        password=args.password,
    )

    def shutdown(sig, frame):
        log.info("Signal %d received, disconnecting...", sig)
        dvr.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        dvr.connect(channel=args.channel, stream_type=args.stream_type)
        log.info("Streaming channel %d to stdout...", args.channel)

        stdout = sys.stdout.buffer
        for _codec, h264_data in dvr.stream():
            try:
                stdout.write(h264_data)
                stdout.flush()
            except BrokenPipeError:
                log.info("Stdout pipe broken — reader disconnected")
                break

    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.error("Fatal: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        dvr.disconnect()


if __name__ == '__main__':
    main()
