# Third-party notices

## The Movie Database (TMDB)

This product uses the TMDB API but is not endorsed or certified by TMDB.

The TMDB logo in `app/static/tmdb-logo.svg` is displayed unmodified for attribution. The file was created by Travis Bell and sourced from The Movie Database through Wikimedia Commons. The Wikimedia copy is marked CC BY-SA 4.0.

- Original TMDB asset: `https://www.themoviedb.org/assets/2/v4/logos/v2/blue_square_2-d537fb228cf3ded904ef09b136fe3fec72548ebc1fea3fbbd1ad9e36364db38b.svg`
- Wikimedia file page: `https://commons.wikimedia.org/wiki/File:Tmdb.new.logo.svg`
- License: `https://creativecommons.org/licenses/by-sa/4.0/`

## FFmpeg / ffprobe

The container installs the Debian `ffmpeg` package and uses its `ffprobe` command to read media stream metadata. MediaLab does not bundle a modified FFmpeg source tree. The package-specific copyright and license information is available inside the built image under `/usr/share/doc/ffmpeg/copyright`.

## FFmpeg / FFprobe

The container installs the Debian `ffmpeg` package and uses `ffprobe` only to read technical stream metadata. FFmpeg is an independent project distributed under its own LGPL/GPL licensing terms. See the FFmpeg project and the package license files included by Debian for the applicable terms.
