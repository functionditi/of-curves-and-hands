This folder contains the passive-mode launcher used by the `mediapipe-handpose` app.

`run_passive_kolam.py` is a thin wrapper around:

`AxiDraw_API_396/sketches-mdw/test-d-1-n-plotter-10min-dynamickolam.py`

The wrapper exists so the browser app and the bridge can reference passive mode from the
`mediapipe-handpose` tree without creating a second copy of the autonomous kolam logic.
