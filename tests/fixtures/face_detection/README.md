# Face-detection fixture

`astronaut.png` is the `scikit-image` 0.25.2 astronaut sample, which depicts
NASA astronaut Eileen Collins. The source is public domain:
https://scikit-image.org/docs/stable/api/skimage.data#skimage.data.astronaut

- Source: https://raw.githubusercontent.com/scikit-image/scikit-image/v0.25.2/skimage/data/astronaut.png
- SHA-256: `88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5`

The YuNet tests derive their rotated, reduced, and two-face cases from this
single checked-in source image so the suite remains local and deterministic.
