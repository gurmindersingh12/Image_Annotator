# Contributing to Image Annotator

For an issue, describe what you were trying to do, which step failed, and what you expected. Include your operating system, browser, app version, and whether you used a desktop build or the source launcher. Try the practice project or a synthetic image when sharing an example.

For a code change, make a branch, run the Python and JavaScript checks in the README, and describe the effect on the person annotating. Add a small regression test when fixing a bug. Changes to image conversion or coordinates should include a small round-trip example that checks saved labels against exported pixels.

The software note and synthetic experiment live alongside the implementation. If the matcher changes, rerun the experiment and explain differences from the recorded results.
