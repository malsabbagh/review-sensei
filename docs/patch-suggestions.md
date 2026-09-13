# Patch suggestions

`review_sensei.patches.create_patch_suggestion` creates a suggestion-only
artifact from a confirmed finding and a bounded unified diff. The artifact is
bound to exact base/head commit identities, validated repository paths, and a
content digest. Suggestions are never self-accepted and have no commit,
publish, or execution operation; a separate human-authorized workflow must
apply one after revalidating the identities and scope.
