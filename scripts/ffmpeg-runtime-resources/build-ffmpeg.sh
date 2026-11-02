#!/bin/sh
set -eu
mkdir -p .fftmp
export TMPDIR=./.fftmp
# Run from the extracted, unmodified FFmpeg 7.1 source directory.
# Put w64devkit 2.11.0 bin first in PATH in this process only.
# All paths recorded by configure are neutral and relative.
./configure \
  --prefix=/usr \
  --target-os=mingw32 --arch=x86_64 --cc=gcc --cxx=g++ \
  --disable-autodetect --disable-gpl --disable-nonfree --disable-version3 \
  --disable-debug --enable-stripping --disable-doc \
  --disable-x86asm --disable-network --disable-hwaccels \
  --disable-avdevice --disable-postproc --disable-ffplay --disable-ffprobe \
  --enable-w32threads --disable-pthreads \
  --extra-cflags="-ffile-prefix-map=.=src -fdebug-prefix-map=.=src -fmacro-prefix-map=.=src" \
  --extra-ldflags="-static -Wl,--no-insert-timestamp"
make V=1 -j8 ffmpeg.exe
strip --strip-all ffmpeg.exe
