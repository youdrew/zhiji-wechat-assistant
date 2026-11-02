# OpenCV FFmpeg source delivery

These are the unchanged official source archives for the dynamically loaded
opencv_videoio_ffmpeg4130_64.dll in opencv-python 4.13.0.90. The DLL's MD5 exactly
matches the immutable OpenCV 4.13.0 download recipe:
https://github.com/opencv/opencv/blob/4.13.0/3rdparty/ffmpeg/ffmpeg.cmake

Binary commit: d82ad9a54a7b42a1648a9cae8fed5c2f20ea396c
Source commit: 62bd53c24feadd92af0146462317fcd4d64fd842
The source commit's direct parent is the binary commit. The archives come from
the upstream ffmpeg/4.x_20251226_src branch, frozen at the above source commit.
Each archive was checked against its Git blob identity and recorded SHA-256.

The supplied upstream archive_src.sh recipe includes FFmpeg n4.4.6, libvpx
v1.15.2, AOM v3.13.1, the OpenH264 v1.8.0 API headers and wrapper/build sources.
We distribute no OpenH264 runtime binary. Retain each component's own license.
Unpack opencv_ffmpeg.tar.xz for the original build/download scripts and Docker
recipe. Arrange the other archives as shown in archive_src.sh: build/ffmpeg,
build/libvpx, build/aom, build/openh264, and opencv. The OpenCV source identity
used by the upstream binary recipe is cff7581175d2abfc6aef2e4f04f482e258b5c864:
https://github.com/opencv/opencv/tree/cff7581175d2abfc6aef2e4f04f482e258b5c864

Original upstream source-delivery recipe:
https://github.com/opencv/opencv_3rdparty/blob/62bd53c24feadd92af0146462317fcd4d64fd842/ffmpeg/archive_src.sh
Original upstream build recipe:
https://github.com/opencv/opencv_3rdparty/blob/d82ad9a54a7b42a1648a9cae8fed5c2f20ea396c/ffmpeg/make_mingw.sh

The application loads this DLL dynamically. It has not been modified by this
project; the separate standalone FFmpeg executable is a different build with
its own source bundle and licenses. These source archives and license/readme
files are provided alongside the binary rather than as an unfulfilled offer.
