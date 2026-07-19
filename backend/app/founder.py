import uuid

# Fixed, non-secret sentinel — not derived from any credential and not generated per
# deployment. Its fixedness is the point: app/deps.py's require_founder() checks the
# authenticated user's id against this exact constant (not just their role), so even a bug
# that let a second row get role=founder (e.g. a bad migration or manual DB edit) couldn't
# grant MainAI access, since that row would have a different primary key. This is a stronger
# guarantee than a role check alone and needs no secret to work — anyone can read this value
# from the repo; only the row it actually points at is privileged. See app/bootstrap.py for
# where the row referenced by this id is created.
FOUNDER_USER_ID = uuid.UUID(int=1)
