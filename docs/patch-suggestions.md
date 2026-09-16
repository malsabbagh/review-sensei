# Patch suggestions

`review_sensei.patches.create_patch_suggestion` creates a suggestion-only
artifact from a confirmed finding and a bounded unified diff. The artifact is
bound to exact base/head commit identities, validated repository paths, a
content digest, and trusted `snapshot_modes` read from the exact reviewed
snapshot. Mode provenance must cover every changed path and identify a regular
Git file; this prevents a mode-less patch from being applied to an existing
symlink. Suggestions are never self-accepted and have no commit, publish, or
execution operation; a separate human-authorized workflow must apply one after
Suggestions never implicitly expand their scope: `allowed_paths` and the
patch's changed paths must exactly match the finding's declared path set.
A suggestion that includes any path outside that finding scope is rejected.
