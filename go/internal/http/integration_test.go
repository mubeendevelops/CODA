//go:build integration

// Integration tests against a real Postgres, per the task instructions.
// They spin up a disposable postgres:16 container via testcontainers-go,
// apply every migration with the same `migrate` CLI the rest of this repo
// uses (go/migrations, scripts/migrate_test.sh), and exercise the full
// router — auth, RBAC, org-scoping, and audit logging — over real HTTP
// against real rows, not mocks.
//
// Run with: cd go && go test -tags=integration ./internal/http/...
// (Makefile target: `make test-integration`.) Requires a working Docker
// daemon; skipped/failed loudly if the `migrate` CLI is not on PATH.
package http

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/testcontainers/testcontainers-go"
	tcpostgres "github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"

	"coda/go/internal/audit"
	"coda/go/internal/auth"
	"coda/go/internal/config"
	"coda/go/internal/db"
	"coda/go/internal/db/sqlc"
)

const integrationTestPassword = "integration-test-password-1"

type testEnv struct {
	server  *httptest.Server
	queries *sqlc.Queries
	ctx     context.Context
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	ctx := context.Background()

	pgContainer, err := tcpostgres.Run(ctx, "postgres:16",
		tcpostgres.WithDatabase("coda_test"),
		tcpostgres.WithUsername("coda_test"),
		tcpostgres.WithPassword("coda_test"),
		// This repo's working tree lives on an NTFS volume (fuseblk),
		// which cannot report real Unix file ownership — Docker's own
		// data root is on the same volume here, so a named/anonymous
		// volume for the data directory makes Postgres refuse to start
		// ("wrong ownership"). tmpfs sidesteps it entirely, exactly like
		// scripts/migrate_test.sh does for the same reason (see
		// claude_context.md §11); it's also the right choice for a
		// throwaway per-test container regardless of filesystem.
		testcontainers.WithTmpfs(map[string]string{"/var/lib/postgresql/data": ""}),
		testcontainers.WithWaitStrategy(
			wait.ForLog("database system is ready to accept connections").
				WithOccurrence(2).WithStartupTimeout(60*time.Second),
		),
	)
	if err != nil {
		t.Fatalf("start postgres container: %v", err)
	}
	t.Cleanup(func() {
		if err := pgContainer.Terminate(context.Background()); err != nil {
			t.Logf("terminate postgres container: %v", err)
		}
	})

	dsn, err := pgContainer.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		t.Fatalf("connection string: %v", err)
	}

	runMigrations(t, dsn)

	pool, err := db.NewPool(ctx, dsn)
	if err != nil {
		t.Fatalf("connect pool: %v", err)
	}
	t.Cleanup(pool.Close)

	queries := sqlc.New(pool)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))

	authCfg := config.Auth{
		JWTSigningKey:   "integration-test-signing-key-32-bytes-long",
		AccessTokenTTL:  15 * time.Minute,
		RefreshTokenTTL: 7 * 24 * time.Hour,
	}
	serverCfg := config.Server{
		HTTPPort:              "0",
		MetricsPort:           "0",
		CORSAllowedOrigins:    []string{"http://localhost"},
		RequestTimeout:        10 * time.Second,
		RateLimitRequests:     100000,
		RateLimitWindow:       time.Minute,
		AuthRateLimitRequests: 100000,
		AuthRateLimitWindow:   time.Minute,
	}

	router := NewRouter(RouterDeps{
		ServiceName: "go-api-integration-test",
		Version:     "test",
		Pool:        pool,
		Queries:     queries,
		JWT:         auth.NewJWTService(authCfg.JWTSigningKey, authCfg.AccessTokenTTL),
		Recorder:    audit.NewSQLRecorder(queries),
		Logger:      logger,
		ServerCfg:   serverCfg,
		AuthCfg:     authCfg,
	})

	server := httptest.NewServer(router)
	t.Cleanup(server.Close)

	return &testEnv{server: server, queries: queries, ctx: ctx}
}

func runMigrations(t *testing.T, dsn string) {
	t.Helper()
	migrateBin, err := exec.LookPath("migrate")
	if err != nil {
		t.Fatalf("golang-migrate CLI not found on PATH: %v (install it — the same tool `make migrate`/`make migrate-test` use)", err)
	}

	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("could not determine test file location")
	}
	migrationsDir := filepath.Join(filepath.Dir(thisFile), "..", "..", "migrations")

	cmd := exec.Command(migrateBin, "-path", migrationsDir, "-database", dsn, "up")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("run migrations: %v\n%s", err, out)
	}
}

// --- fixture helpers -------------------------------------------------

func mustCreateOrg(t *testing.T, env *testEnv, name string) *sqlc.Org {
	t.Helper()
	org, err := env.queries.CreateOrg(env.ctx, sqlc.CreateOrgParams{Name: name, Region: "IN"})
	if err != nil {
		t.Fatalf("create org %q: %v", name, err)
	}
	return org
}

func mustCreateUser(t *testing.T, env *testEnv, orgID uuid.UUID, email string, role auth.Role) *sqlc.User {
	t.Helper()
	hash, err := auth.HashPassword(integrationTestPassword)
	if err != nil {
		t.Fatalf("hash password: %v", err)
	}
	user, err := env.queries.CreateUser(env.ctx, sqlc.CreateUserParams{
		OrgID: orgID, Email: email, PasswordHash: hash, Role: string(role),
	})
	if err != nil {
		t.Fatalf("create user %q: %v", email, err)
	}
	return user
}

func mustCreateConsultation(t *testing.T, env *testEnv, orgID, ownerID uuid.UUID, language string) *sqlc.Consultation {
	t.Helper()
	grantedAt := pgtype.Timestamptz{Time: time.Now().UTC(), Valid: true}
	consent, err := env.queries.CreateConsentRecord(env.ctx, sqlc.CreateConsentRecordParams{
		OrgID: orgID, SubjectRef: "it-subject-" + uuid.NewString(), ConsentType: "recording",
		GrantedAt: grantedAt, GrantedBy: ownerID,
	})
	if err != nil {
		t.Fatalf("create consent record: %v", err)
	}
	c, err := env.queries.CreateConsultation(env.ctx, sqlc.CreateConsultationParams{
		OrgID: orgID, OwnerUserID: ownerID, ConsentRecordID: consent.ID, ConsentObtained: true,
		ConsentMethod: pgtype.Text{String: "verbal", Valid: true}, ConsentRecordedAt: grantedAt, Language: language,
	})
	if err != nil {
		t.Fatalf("create consultation: %v", err)
	}
	return c
}

// --- HTTP helpers ------------------------------------------------------

func (env *testEnv) doJSON(t *testing.T, method, path, token string, body any) *http.Response {
	t.Helper()
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal body: %v", err)
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, env.server.URL+path, reader)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request %s %s: %v", method, path, err)
	}
	return resp
}

func decodeJSON[T any](t *testing.T, resp *http.Response) T {
	t.Helper()
	defer resp.Body.Close()
	var v T
	if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	return v
}

func (env *testEnv) login(t *testing.T, email, password string) tokenResponse {
	t.Helper()
	resp := env.doJSON(t, http.MethodPost, "/v1/auth/login", "", loginRequest{Email: email, Password: password})
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("login as %s failed: %d %s", email, resp.StatusCode, b)
	}
	return decodeJSON[tokenResponse](t, resp)
}

// --- tests ---------------------------------------------------------------

func TestIntegration_RegisterIsAdminOnly(t *testing.T) {
	env := newTestEnv(t)
	org := mustCreateOrg(t, env, "Register Test Org")
	admin := mustCreateUser(t, env, org.ID, "admin@register-test.dev", auth.RoleAdmin)
	doctor := mustCreateUser(t, env, org.ID, "doctor@register-test.dev", auth.RoleDoctor)
	_ = doctor

	adminTokens := env.login(t, admin.Email, integrationTestPassword)
	doctorTokens := env.login(t, "doctor@register-test.dev", integrationTestPassword)

	newUser := registerRequest{Email: fmt.Sprintf("new-%s@register-test.dev", uuid.NewString()), Password: "a-strong-password", Role: "doctor"}

	t.Run("unauthenticated is rejected", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/register", "", newUser)
		if resp.StatusCode != http.StatusUnauthorized {
			t.Errorf("status = %d, want 401", resp.StatusCode)
		}
	})

	t.Run("non-admin is rejected", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/register", doctorTokens.AccessToken, newUser)
		if resp.StatusCode != http.StatusForbidden {
			t.Errorf("status = %d, want 403", resp.StatusCode)
		}
	})

	t.Run("admin can register a user into their own org", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/register", adminTokens.AccessToken, newUser)
		if resp.StatusCode != http.StatusCreated {
			b, _ := io.ReadAll(resp.Body)
			t.Fatalf("status = %d, want 201: %s", resp.StatusCode, b)
		}
		created := decodeJSON[userResponse](t, resp)
		if created.OrgID != org.ID {
			t.Errorf("created user org = %v, want %v (admin's own org)", created.OrgID, org.ID)
		}
	})

	t.Run("duplicate email is rejected", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/register", adminTokens.AccessToken, newUser)
		if resp.StatusCode != http.StatusConflict {
			t.Errorf("status = %d, want 409", resp.StatusCode)
		}
	})

	t.Run("invalid role is rejected", func(t *testing.T) {
		bad := registerRequest{Email: "bad-role@register-test.dev", Password: "a-strong-password", Role: "superuser"}
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/register", adminTokens.AccessToken, bad)
		if resp.StatusCode != http.StatusBadRequest {
			t.Errorf("status = %d, want 400", resp.StatusCode)
		}
	})
}

func TestIntegration_Login(t *testing.T) {
	env := newTestEnv(t)
	org := mustCreateOrg(t, env, "Login Test Org")
	user := mustCreateUser(t, env, org.ID, "login-user@login-test.dev", auth.RoleDoctor)

	t.Run("correct credentials succeed", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/login", "", loginRequest{Email: user.Email, Password: integrationTestPassword})
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		tokens := decodeJSON[tokenResponse](t, resp)
		if tokens.AccessToken == "" || tokens.RefreshToken == "" {
			t.Error("expected non-empty tokens")
		}
	})

	t.Run("wrong password is rejected", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/login", "", loginRequest{Email: user.Email, Password: "wrong-password"})
		if resp.StatusCode != http.StatusUnauthorized {
			t.Errorf("status = %d, want 401", resp.StatusCode)
		}
	})

	t.Run("unknown email is rejected", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/login", "", loginRequest{Email: "nobody@login-test.dev", Password: "whatever"})
		if resp.StatusCode != http.StatusUnauthorized {
			t.Errorf("status = %d, want 401", resp.StatusCode)
		}
	})
}

func TestIntegration_RefreshRotationAndReuseDetection(t *testing.T) {
	env := newTestEnv(t)
	org := mustCreateOrg(t, env, "Refresh Test Org")
	user := mustCreateUser(t, env, org.ID, "refresh-user@refresh-test.dev", auth.RoleDoctor)

	original := env.login(t, user.Email, integrationTestPassword)

	rotated := decodeJSON[tokenResponse](t, env.doJSON(t, http.MethodPost, "/v1/auth/refresh", "", refreshRequest{RefreshToken: original.RefreshToken}))
	if rotated.RefreshToken == "" || rotated.RefreshToken == original.RefreshToken {
		t.Fatal("expected a freshly rotated, distinct refresh token")
	}

	t.Run("reusing the original (already-rotated) token is rejected", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/refresh", "", refreshRequest{RefreshToken: original.RefreshToken})
		if resp.StatusCode != http.StatusUnauthorized {
			t.Errorf("status = %d, want 401", resp.StatusCode)
		}
	})

	t.Run("reuse revokes the whole family, burning the rotated token too", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/refresh", "", refreshRequest{RefreshToken: rotated.RefreshToken})
		if resp.StatusCode != http.StatusUnauthorized {
			t.Errorf("status = %d, want 401 — reuse of an old token must revoke the whole rotation chain", resp.StatusCode)
		}
	})
}

func TestIntegration_Logout(t *testing.T) {
	env := newTestEnv(t)
	org := mustCreateOrg(t, env, "Logout Test Org")
	user := mustCreateUser(t, env, org.ID, "logout-user@logout-test.dev", auth.RoleDoctor)

	tokens := env.login(t, user.Email, integrationTestPassword)

	resp := env.doJSON(t, http.MethodPost, "/v1/auth/logout", "", refreshRequest{RefreshToken: tokens.RefreshToken})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("logout status = %d, want 200", resp.StatusCode)
	}

	t.Run("refreshing a logged-out token is rejected", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/refresh", "", refreshRequest{RefreshToken: tokens.RefreshToken})
		if resp.StatusCode != http.StatusUnauthorized {
			t.Errorf("status = %d, want 401", resp.StatusCode)
		}
	})

	t.Run("logout is idempotent", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodPost, "/v1/auth/logout", "", refreshRequest{RefreshToken: tokens.RefreshToken})
		if resp.StatusCode != http.StatusOK {
			t.Errorf("status = %d, want 200", resp.StatusCode)
		}
	})
}

func TestIntegration_RBACRoleDenials(t *testing.T) {
	env := newTestEnv(t)
	org := mustCreateOrg(t, env, "RBAC Test Org")

	roles := map[auth.Role]*sqlc.User{
		auth.RoleAdmin:    mustCreateUser(t, env, org.ID, "rbac-admin@rbac-test.dev", auth.RoleAdmin),
		auth.RoleDoctor:   mustCreateUser(t, env, org.ID, "rbac-doctor@rbac-test.dev", auth.RoleDoctor),
		auth.RoleReviewer: mustCreateUser(t, env, org.ID, "rbac-reviewer@rbac-test.dev", auth.RoleReviewer),
		auth.RoleAuditor:  mustCreateUser(t, env, org.ID, "rbac-auditor@rbac-test.dev", auth.RoleAuditor),
	}
	tokens := make(map[auth.Role]string, len(roles))
	for role, user := range roles {
		tokens[role] = env.login(t, user.Email, integrationTestPassword).AccessToken
	}

	tests := []struct {
		name       string
		role       auth.Role
		method     string
		path       string
		wantStatus int
	}{
		{"admin reads users", auth.RoleAdmin, http.MethodGet, "/v1/users", http.StatusOK},
		{"doctor cannot read users", auth.RoleDoctor, http.MethodGet, "/v1/users", http.StatusForbidden},
		{"auditor cannot read users", auth.RoleAuditor, http.MethodGet, "/v1/users", http.StatusForbidden},

		{"doctor reads consultations", auth.RoleDoctor, http.MethodGet, "/v1/consultations", http.StatusOK},
		{"reviewer reads consultations", auth.RoleReviewer, http.MethodGet, "/v1/consultations", http.StatusOK},
		{"admin reads consultations", auth.RoleAdmin, http.MethodGet, "/v1/consultations", http.StatusOK},
		{"auditor cannot read consultations", auth.RoleAuditor, http.MethodGet, "/v1/consultations", http.StatusForbidden},

		{"admin reads audit log", auth.RoleAdmin, http.MethodGet, "/v1/audit-log", http.StatusOK},
		{"auditor reads audit log", auth.RoleAuditor, http.MethodGet, "/v1/audit-log", http.StatusOK},
		{"doctor cannot read audit log", auth.RoleDoctor, http.MethodGet, "/v1/audit-log", http.StatusForbidden},
		{"reviewer cannot read audit log", auth.RoleReviewer, http.MethodGet, "/v1/audit-log", http.StatusForbidden},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			resp := env.doJSON(t, tt.method, tt.path, tokens[tt.role], nil)
			if resp.StatusCode != tt.wantStatus {
				t.Errorf("status = %d, want %d", resp.StatusCode, tt.wantStatus)
			}
		})
	}
}

// TestIntegration_OrgScopingCrossOrgReadIsDenied is the adversarial test
// the task instructions specifically ask for: a user from one org must
// never be able to read another org's data through this API, no matter
// what ID it asks for by name.
func TestIntegration_OrgScopingCrossOrgReadIsDenied(t *testing.T) {
	env := newTestEnv(t)

	orgA := mustCreateOrg(t, env, "Org A")
	orgB := mustCreateOrg(t, env, "Org B")

	doctorA := mustCreateUser(t, env, orgA.ID, "doctor@org-a.dev", auth.RoleDoctor)
	doctorB := mustCreateUser(t, env, orgB.ID, "doctor@org-b.dev", auth.RoleDoctor)
	adminB := mustCreateUser(t, env, orgB.ID, "admin@org-b.dev", auth.RoleAdmin)

	consultA := mustCreateConsultation(t, env, orgA.ID, doctorA.ID, "en")

	tokensB := env.login(t, doctorB.Email, integrationTestPassword)
	adminTokensB := env.login(t, adminB.Email, integrationTestPassword)

	t.Run("org B doctor cannot fetch org A's consultation by ID", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodGet, "/v1/consultations/"+consultA.ID.String(), tokensB.AccessToken, nil)
		if resp.StatusCode != http.StatusNotFound {
			t.Errorf("status = %d, want 404 (cross-org existence must not leak as 403)", resp.StatusCode)
		}
	})

	t.Run("org B doctor's consultation list never contains org A's consultation", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodGet, "/v1/consultations", tokensB.AccessToken, nil)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		list := decodeJSON[[]consultationResponse](t, resp)
		for _, c := range list {
			if c.ID == consultA.ID {
				t.Fatalf("org B's list leaked org A's consultation %v", c.ID)
			}
			if c.OrgID != orgB.ID {
				t.Fatalf("list returned a consultation from org %v while authenticated as org %v", c.OrgID, orgB.ID)
			}
		}
	})

	t.Run("org B admin's user list never contains org A's doctor", func(t *testing.T) {
		resp := env.doJSON(t, http.MethodGet, "/v1/users", adminTokensB.AccessToken, nil)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		list := decodeJSON[[]userResponse](t, resp)
		for _, u := range list {
			if u.ID == doctorA.ID {
				t.Fatalf("org B's user list leaked org A's doctor %v", u.ID)
			}
		}
	})

	t.Run("org B admin's audit log never contains org A's entries", func(t *testing.T) {
		// Generate at least one org A audit entry first.
		tokensA := env.login(t, doctorA.Email, integrationTestPassword)
		env.doJSON(t, http.MethodGet, "/v1/consultations/"+consultA.ID.String(), tokensA.AccessToken, nil).Body.Close()

		resp := env.doJSON(t, http.MethodGet, "/v1/audit-log", adminTokensB.AccessToken, nil)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		list := decodeJSON[[]auditLogEntryResponse](t, resp)
		for _, e := range list {
			if e.OrgID != orgB.ID {
				t.Fatalf("org B's audit log leaked an entry from org %v", e.OrgID)
			}
		}
	})
}

func TestIntegration_AuditLogRecordsMutationsAndClinicalReads(t *testing.T) {
	env := newTestEnv(t)
	org := mustCreateOrg(t, env, "Audit Test Org")
	admin := mustCreateUser(t, env, org.ID, "audit-admin@audit-test.dev", auth.RoleAdmin)
	doctor := mustCreateUser(t, env, org.ID, "audit-doctor@audit-test.dev", auth.RoleDoctor)
	consult := mustCreateConsultation(t, env, org.ID, doctor.ID, "en")

	adminTokens := env.login(t, admin.Email, integrationTestPassword)
	doctorTokens := env.login(t, doctor.Email, integrationTestPassword)

	// A mutating request (register) and a clinical-data read (get
	// consultation) — the two categories the task instructions require to
	// always be audited.
	newUserEmail := fmt.Sprintf("registered-%s@audit-test.dev", uuid.NewString())
	env.doJSON(t, http.MethodPost, "/v1/auth/register", adminTokens.AccessToken,
		registerRequest{Email: newUserEmail, Password: "a-strong-password", Role: "doctor"}).Body.Close()

	env.doJSON(t, http.MethodGet, "/v1/consultations/"+consult.ID.String(), doctorTokens.AccessToken, nil).Body.Close()

	resp := env.doJSON(t, http.MethodGet, "/v1/audit-log", adminTokens.AccessToken, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	entries := decodeJSON[[]auditLogEntryResponse](t, resp)

	var sawRegister, sawClinicalRead, sawLogin bool
	for _, e := range entries {
		switch {
		case e.Action == "user.register":
			sawRegister = true
			if e.Outcome != "success" {
				t.Errorf("user.register outcome = %s, want success", e.Outcome)
			}
			if e.ActorUserID == nil || *e.ActorUserID != admin.ID {
				t.Errorf("user.register actor = %v, want %v", e.ActorUserID, admin.ID)
			}
		case e.Action == "consultation.read":
			sawClinicalRead = true
			if e.ResourceType != "consultation" || e.ResourceID != consult.ID.String() {
				t.Errorf("consultation.read resource = %s/%s, want consultation/%s", e.ResourceType, e.ResourceID, consult.ID)
			}
		case e.Action == "auth.login":
			sawLogin = true
		}
		if e.OrgID != org.ID {
			t.Errorf("entry org = %v, want %v", e.OrgID, org.ID)
		}
	}

	if !sawRegister {
		t.Error("expected an audit entry for user.register (mutating request)")
	}
	if !sawClinicalRead {
		t.Error("expected an audit entry for consultation.read (clinical data read)")
	}
	if !sawLogin {
		t.Error("expected an audit entry for auth.login")
	}
}
