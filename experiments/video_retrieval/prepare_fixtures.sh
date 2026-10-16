#!/usr/bin/env bash
set -euo pipefail

experiment_root="${1:-/home/dev/research/video-moment-retrieval-20261016/experiment}"
source_dir="${experiment_root}/fixtures/source"
derived_dir="${experiment_root}/fixtures/derived"
evidence_dir="${experiment_root}/evidence"

mkdir -p "${source_dir}" "${derived_dir}" "${evidence_dir}"

fetch() {
  local filename="$1"
  local expected="$2"
  local url="$3"
  if [[ ! -f "${source_dir}/${filename}" ]]; then
    curl -fL --retry 3 --output "${source_dir}/${filename}" "${url}"
  fi
  printf '%s  %s\n' "${expected}" "${source_dir}/${filename}" | sha256sum --check --status
}

fetch \
  pasta-boiling.webm \
  5cf36241df2e9c3e096b9e159dd4bab3343aad7624c1e2eb76bf2cf549289b77 \
  https://upload.wikimedia.org/wikipedia/commons/7/7e/Pot_of_pasta_boiling.webm
fetch \
  go-train.webm \
  224db2c5024a84106f7525a934a5a78b1d3edf1f27c2e294d945cb015b4c0163 \
  https://upload.wikimedia.org/wikipedia/commons/2/29/GO_Train_at_level_crossing.webm
fetch \
  horses-running.webm \
  a845625850b2b899fb70e377154cc1a20e8f977f7a0542ba6ec5f6dbcd636ff4 \
  https://upload.wikimedia.org/wikipedia/commons/a/af/Horses_running_in_a_pasture.webm
fetch \
  librispeech-1688-142285-0007.wav \
  67dfc27041e572bea27936e75eedc542ccf6f3af5cc251a0ab4e1e947d787fc8 \
  https://download.pytorch.org/torchaudio/tutorial-assets/VOiCES_devkit/source-16k/1688-142285-0007.wav

ffmpeg -y -loglevel error \
  -i "${source_dir}/librispeech-1688-142285-0007.wav" \
  -ac 1 -ar 16000 -c:a pcm_s16le \
  "${source_dir}/librispeech-1688-142285-0007-pcm16.wav"
printf '%s  %s\n' \
  8295590232103d73cf809dcc118fb5f20e0415b1440f46a38b2354f9f1630a5b \
  "${source_dir}/librispeech-1688-142285-0007-pcm16.wav" | sha256sum --check --status

ffmpeg -y -loglevel error \
  -i "${source_dir}/pasta-boiling.webm" \
  -i "${source_dir}/go-train.webm" \
  -ss 30 -i "${source_dir}/horses-running.webm" \
  -filter_complex \
  '[0:v]trim=start=0:end=5,setpts=PTS-STARTPTS,fps=10,scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:black,setsar=1[p];[1:v]trim=start=0:end=20,setpts=PTS-STARTPTS,fps=10,scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:black,setsar=1[t];[2:v]trim=start=0:end=15,setpts=PTS-STARTPTS,fps=10,scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:black,setsar=1[h];[p][t][h]concat=n=3:v=1:a=0[v]' \
  -map '[v]' -an -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
  "${derived_dir}/composite-silent-cfr.mp4"

ffmpeg -y -loglevel error \
  -i "${derived_dir}/composite-silent-cfr.mp4" \
  -f lavfi -t 40 -i anullsrc=r=16000:cl=mono \
  -itsoffset 4.2 -i "${source_dir}/librispeech-1688-142285-0007-pcm16.wav" \
  -filter_complex '[1:a][2:a]amix=inputs=2:duration=first:normalize=0[a]' \
  -map 0:v -map '[a]' -c:v copy -c:a pcm_s16le \
  "${derived_dir}/composite-cfr.mkv"

ffmpeg -y -loglevel error \
  -i "${derived_dir}/composite-cfr.mkv" \
  -vf "select='if(lt(mod(t,1),0.5),not(mod(n,2)),not(mod(n,5)))'" \
  -fps_mode vfr -c:v libx264 -preset veryfast -crf 20 -c:a copy \
  "${derived_dir}/composite-vfr.mkv"

ffmpeg -y -loglevel error \
  -ss 40 -t 5 -i "${source_dir}/horses-running.webm" \
  -vf 'fps=10,scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:black,setsar=1' \
  -an -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
  "${derived_dir}/horses-forward-5s.mkv"

ffmpeg -y -loglevel error \
  -i "${derived_dir}/horses-forward-5s.mkv" \
  -vf reverse -an -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
  "${derived_dir}/horses-reversed-5s.mkv"

ffprobe -v error -show_format -show_streams -of json \
  "${derived_dir}/composite-cfr.mkv" > "${evidence_dir}/composite-cfr.ffprobe.json"
ffprobe -v error -show_format -show_streams -of json \
  "${derived_dir}/composite-vfr.mkv" > "${evidence_dir}/composite-vfr.ffprobe.json"
ffprobe -v error -show_format -show_streams -of json \
  "${derived_dir}/horses-forward-5s.mkv" > "${evidence_dir}/horses-forward.ffprobe.json"
ffprobe -v error -show_format -show_streams -of json \
  "${derived_dir}/horses-reversed-5s.mkv" > "${evidence_dir}/horses-reversed.ffprobe.json"

sha256sum "${source_dir}"/* "${derived_dir}"/* \
  | tee "${evidence_dir}/fixture-sha256.txt"
