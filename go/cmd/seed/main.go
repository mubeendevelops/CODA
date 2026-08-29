// Command seed populates a fresh database with one org, one user per RBAC
// role (admin/doctor/reviewer/auditor — go/internal/auth/roles.go), and two
// sample consultations — enough to exercise the review UI, the API, and
// RBAC/org-scoping by hand once Phases 3/8 exist. Idempotent: re-running it
// finds the existing org/users by their fixed dev identifiers and skips
// creating them again, rather than failing on the email UNIQUE constraint.
// It always inserts two fresh consultations, though, since re-seeding is
// expected to be cheap and there is no natural key to dedupe consultations
// on.
package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"coda/go/internal/auth"
	"coda/go/internal/config"
	"coda/go/internal/db"
	"coda/go/internal/db/sqlc"
)

const (
	seedOrgName       = "CODA Demo Clinic"
	seedDoctorEmail   = "doctor@coda.dev"
	seedAdminEmail    = "admin@coda.dev"
	seedReviewerEmail = "reviewer@coda.dev"
	seedAuditorEmail  = "auditor@coda.dev"
	seedDevPassword   = "coda-dev-password" // dev-only seed credential, not a secret
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	ctx := context.Background()

	dbCfg := config.LoadDB()
	pool, err := db.NewPool(ctx, dbCfg.DSN())
	if err != nil {
		logger.Error("connect", "error", err)
		os.Exit(1)
	}
	defer pool.Close()

	q := sqlc.New(pool)

	org, err := getOrCreateOrg(ctx, q, logger)
	if err != nil {
		logger.Error("seed org", "error", err)
		os.Exit(1)
	}

	passwordHash, err := auth.HashPassword(seedDevPassword)
	if err != nil {
		logger.Error("hash password", "error", err)
		os.Exit(1)
	}

	doctor, err := getOrCreateUser(ctx, q, logger, org.ID, seedDoctorEmail, string(auth.RoleDoctor), passwordHash)
	if err != nil {
		logger.Error("seed doctor user", "error", err)
		os.Exit(1)
	}

	admin, err := getOrCreateUser(ctx, q, logger, org.ID, seedAdminEmail, string(auth.RoleAdmin), passwordHash)
	if err != nil {
		logger.Error("seed admin user", "error", err)
		os.Exit(1)
	}

	if _, err := getOrCreateUser(ctx, q, logger, org.ID, seedReviewerEmail, string(auth.RoleReviewer), passwordHash); err != nil {
		logger.Error("seed reviewer user", "error", err)
		os.Exit(1)
	}

	if _, err := getOrCreateUser(ctx, q, logger, org.ID, seedAuditorEmail, string(auth.RoleAuditor), passwordHash); err != nil {
		logger.Error("seed auditor user", "error", err)
		os.Exit(1)
	}

	samples := []struct {
		subjectRef string
		language   string
	}{
		{"seed-subject-01", "en"},
		{"seed-subject-02", "kn_en"},
	}

	for _, s := range samples {
		consult, err := createConsentedConsultation(ctx, q, org.ID, doctor.ID, s.subjectRef, s.language)
		if err != nil {
			logger.Error("seed consultation", "subject_ref", s.subjectRef, "error", err)
			os.Exit(1)
		}
		logger.Info("consultation seeded", "id", consult.ID, "language", consult.Language)
	}

	logger.Info("seed complete",
		"org_id", org.ID,
		"doctor_email", seedDoctorEmail,
		"admin_id", admin.ID,
		"admin_email", seedAdminEmail,
		"dev_password", seedDevPassword,
	)
}

func getOrCreateOrg(ctx context.Context, q *sqlc.Queries, logger *slog.Logger) (*sqlc.Org, error) {
	if existing, err := q.GetOrgByName(ctx, seedOrgName); err == nil {
		logger.Info("org already seeded", "id", existing.ID)
		return existing, nil
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}

	org, err := q.CreateOrg(ctx, sqlc.CreateOrgParams{Name: seedOrgName, Region: "IN"})
	if err != nil {
		return nil, err
	}
	logger.Info("org created", "id", org.ID)
	return org, nil
}

func getOrCreateUser(
	ctx context.Context, q *sqlc.Queries, logger *slog.Logger,
	orgID uuid.UUID, email, role, passwordHash string,
) (*sqlc.User, error) {
	if existing, err := q.GetUserByEmail(ctx, email); err == nil {
		logger.Info("user already seeded", "email", email, "id", existing.ID)
		return existing, nil
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}

	user, err := q.CreateUser(ctx, sqlc.CreateUserParams{
		OrgID:        orgID,
		Email:        email,
		PasswordHash: passwordHash,
		Role:         role,
	})
	if err != nil {
		return nil, err
	}
	logger.Info("user created", "email", email, "id", user.ID, "role", role)
	return user, nil
}

// createConsentedConsultation records a consent grant before the
// consultation itself, since consultations.consent_record_id is a NOT NULL
// foreign key (docs/architecture.md §7.2) — the two rows are created
// together here rather than exposed as a single combined query, because
// nothing else in the schema needs that shortcut.
func createConsentedConsultation(
	ctx context.Context, q *sqlc.Queries,
	orgID, doctorID uuid.UUID, subjectRef, language string,
) (*sqlc.Consultation, error) {
	var grantedAt pgtype.Timestamptz
	if err := grantedAt.Scan(time.Now().UTC()); err != nil {
		return nil, err
	}

	consent, err := q.CreateConsentRecord(ctx, sqlc.CreateConsentRecordParams{
		OrgID:       orgID,
		SubjectRef:  subjectRef,
		ConsentType: "recording",
		GrantedAt:   grantedAt,
		GrantedBy:   doctorID,
	})
	if err != nil {
		return nil, err
	}

	consentMethod := pgtype.Text{String: "verbal", Valid: true}
	consentRecordedAt := grantedAt

	return q.CreateConsultation(ctx, sqlc.CreateConsultationParams{
		OrgID:             orgID,
		OwnerUserID:       doctorID,
		ConsentRecordID:   consent.ID,
		ConsentObtained:   true,
		ConsentMethod:     consentMethod,
		ConsentRecordedAt: consentRecordedAt,
		Language:          language,
	})
}
