package tasks

import (
	"github.com/google/uuid"

	codev1 "coda/go/internal/genproto/coda/v1"
)

// uuidLike keeps the narrow interfaces above readable.
type uuidLike = uuid.UUID

// stageNLP is the stage whose visibility timeout bounds "how long may a job
// legitimately sit untouched" — the GoT NLP arm's 90-minute window is the
// longest in docs/architecture.md §4.2.
const stageNLP = codev1.Stage_STAGE_NLP
