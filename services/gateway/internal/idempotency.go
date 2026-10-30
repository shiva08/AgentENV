package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
)

const (
	headerIdempotencyKey     = "Idempotency-Key"
	headerIdempotentReplayed = "Idempotent-Replayed"
	idempotencyKeyPrefix     = "agentenv:gateway:idempotency:"
	defaultIdempotencyKeyTTL = 5 * 24 * time.Hour
	idempotencyPollInterval  = 200 * time.Millisecond
	maxIdempotencyKeyLen     = 255
	maxIdempotentBodySize    = 1 << 20
)

// idempotencyRecord is stored under the client's key. Status stays 0 while
// the first request is still creating the sandbox.
type idempotencyRecord struct {
	RequestHash string `json:"request_hash"`
	Status      int    `json:"status,omitempty"`
	ContentType string `json:"content_type,omitempty"`
	SandboxID   string `json:"sandbox_id,omitempty"`
	Body        []byte `json:"body,omitempty"`
}

func (s *Server) isSandboxCreate(r *http.Request) bool {
	if r.Method != http.MethodPost || hasProxyRoutingHeaders(r.Header) {
		return false
	}
	if route, err := parseHostRoute(r.Host, s.sandboxProxyDomains); route != nil || err != nil {
		return false
	}
	switch strings.TrimRight(r.URL.Path, "/") {
	case "/sandboxes", "/v2/sandboxes", "/sandboxes-cold":
		return true
	}
	return false
}

func validIdempotencyKey(key string) bool {
	if key == "" || len(key) > maxIdempotencyKeyLen {
		return false
	}
	for i := 0; i < len(key); i++ {
		if key[i] < '!' || key[i] > '~' {
			return false
		}
	}
	return true
}

// pendingTTL bounds how long an unfinished claim blocks retries if the
// gateway that owns it dies mid-create.
func (s *Server) pendingTTL() time.Duration {
	if s.requestTimeout > 0 {
		return s.requestTimeout + time.Minute
	}
	return 10 * time.Minute
}

// handleIdempotentCreate lets one request per key create a sandbox. Retries
// with the same key and body get that request's response instead.
func (s *Server) handleIdempotentCreate(w http.ResponseWriter, r *http.Request, key string) {
	if !validIdempotencyKey(key) {
		http.Error(w, "Idempotency-Key must be 1 to 255 printable ASCII characters", http.StatusBadRequest)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxIdempotentBodySize+1))
	if err != nil {
		http.Error(w, "failed to read request body", http.StatusBadRequest)
		return
	}
	if len(body) > maxIdempotentBodySize {
		http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		return
	}
	sum := sha256.Sum256(append([]byte(r.Method+" "+strings.TrimRight(r.URL.Path, "/")+"\n"), body...))
	hash := hex.EncodeToString(sum[:])
	redisKey := idempotencyKeyPrefix + key
	pending, err := json.Marshal(idempotencyRecord{RequestHash: hash})
	if err != nil {
		http.Error(w, "failed to encode idempotency record", http.StatusInternalServerError)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), s.pendingTTL())
	defer cancel()
	for {
		claimed, err := s.idempotency.SetNX(ctx, redisKey, pending, s.pendingTTL()).Result()
		if err != nil {
			s.logger.Warn("claim idempotency key failed", zap.Error(err))
			http.Error(w, "idempotency store unavailable", http.StatusServiceUnavailable)
			return
		}
		if claimed {
			s.createOnce(w, r, body, redisKey, hash)
			return
		}

		raw, err := s.idempotency.Get(ctx, redisKey).Bytes()
		if errors.Is(err, redis.Nil) {
			continue
		}
		if err != nil {
			s.logger.Warn("read idempotency key failed", zap.Error(err))
			http.Error(w, "idempotency store unavailable", http.StatusServiceUnavailable)
			return
		}
		var record idempotencyRecord
		if err := json.Unmarshal(raw, &record); err != nil {
			http.Error(w, "corrupt idempotency record", http.StatusInternalServerError)
			return
		}
		if record.RequestHash != hash {
			http.Error(w, "Idempotency-Key was already used with a different request", http.StatusUnprocessableEntity)
			return
		}
		if record.Status != 0 {
			writeIdempotentReplay(w, record)
			return
		}
		select {
		case <-ctx.Done():
			http.Error(w, "a request with this Idempotency-Key is still in progress", http.StatusConflict)
			return
		case <-time.After(idempotencyPollInterval):
		}
	}
}

// createOnce forwards the create that won the claim and records its result.
// It finishes even if the client disconnects, so a retry can replay it.
func (s *Server) createOnce(w http.ResponseWriter, r *http.Request, body []byte, redisKey, hash string) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), s.pendingTTL())
	defer cancel()
	req := r.Clone(ctx)
	req.Body = io.NopCloser(bytes.NewReader(body))
	req.ContentLength = int64(len(body))
	captured := &capturedResponse{header: make(http.Header)}
	s.handleProxy(captured, req)

	if captured.status >= 200 && captured.status < 300 {
		record, err := json.Marshal(idempotencyRecord{
			RequestHash: hash,
			Status:      captured.status,
			ContentType: captured.header.Get("Content-Type"),
			SandboxID:   captured.header.Get(headerSandboxID),
			Body:        captured.body.Bytes(),
		})
		if err == nil {
			err = s.idempotency.Set(ctx, redisKey, record, s.idempotencyTTL).Err()
		}
		if err != nil {
			s.logger.Warn("save idempotency record failed", zap.Error(err))
		}
	} else {
		// Release the key so a retry can try again; an upstream timeout may still finish later.
		if err := s.idempotency.Del(ctx, redisKey).Err(); err != nil {
			s.logger.Warn("release idempotency key failed", zap.Error(err))
		}
	}

	for name, values := range captured.header {
		w.Header()[name] = values
	}
	if captured.status == 0 {
		captured.status = http.StatusBadGateway
	}
	w.WriteHeader(captured.status)
	_, _ = w.Write(captured.body.Bytes())
}

func writeIdempotentReplay(w http.ResponseWriter, record idempotencyRecord) {
	if record.ContentType != "" {
		w.Header().Set("Content-Type", record.ContentType)
	}
	if record.SandboxID != "" {
		w.Header().Set(headerSandboxID, record.SandboxID)
	}
	w.Header().Set(headerIdempotentReplayed, "true")
	w.WriteHeader(record.Status)
	_, _ = w.Write(record.Body)
}

// capturedResponse buffers the upstream reply so it is stored before the client sees it.
type capturedResponse struct {
	header http.Header
	status int
	body   bytes.Buffer
}

func (c *capturedResponse) Header() http.Header {
	return c.header
}

func (c *capturedResponse) WriteHeader(status int) {
	if c.status == 0 {
		c.status = status
	}
}

func (c *capturedResponse) Write(p []byte) (int, error) {
	if c.status == 0 {
		c.status = http.StatusOK
	}
	return c.body.Write(p)
}
