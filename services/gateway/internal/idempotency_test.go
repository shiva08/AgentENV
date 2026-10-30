package gateway

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	schedulerv1 "agentenv/services/api/proto"

	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
)

func startIdempotencyRedis(t *testing.T) *redis.Client {
	t.Helper()
	bin := strings.TrimSpace(os.Getenv("REDIS_SERVER_BIN"))
	if bin == "" {
		var err error
		if bin, err = exec.LookPath("redis-server"); err != nil {
			t.Skip("redis-server not found; set REDIS_SERVER_BIN or install redis-server")
		}
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("allocate redis port failed: %v", err)
	}
	_, port, _ := net.SplitHostPort(listener.Addr().String())
	_ = listener.Close()

	cmd := exec.Command(bin, "--bind", "127.0.0.1", "--port", port, "--save", "", "--appendonly", "no", "--dir", t.TempDir())
	if err := cmd.Start(); err != nil {
		t.Fatalf("start redis-server failed: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})

	client := redis.NewClient(&redis.Options{Addr: net.JoinHostPort("127.0.0.1", port)})
	t.Cleanup(func() { _ = client.Close() })
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(25 * time.Millisecond) {
		if client.Ping(context.Background()).Err() == nil {
			return client
		}
	}
	t.Fatal("redis-server did not become ready")
	return nil
}

// newIdempotencyGateway routes every create to upstream and returns the gateway URL.
func newIdempotencyGateway(t *testing.T, upstream http.Handler, opts ...testServerOption) string {
	t.Helper()
	node := httptest.NewServer(upstream)
	t.Cleanup(node.Close)
	scheduler := stubSchedulerClient{
		scheduleFunc: func(context.Context, *schedulerv1.ScheduleRequest, ...grpc.CallOption) (*schedulerv1.ScheduleResponse, error) {
			return &schedulerv1.ScheduleResponse{Node: &schedulerv1.Node{NodeId: "node-1", Endpoint: node.URL}}, nil
		},
		recordAssignmentFunc: func(context.Context, *schedulerv1.RecordAssignmentRequest, ...grpc.CallOption) (*schedulerv1.RecordAssignmentResponse, error) {
			return &schedulerv1.RecordAssignmentResponse{}, nil
		},
	}
	redisClient := startIdempotencyRedis(t)
	server := newTestServer(t, scheduler, 5*time.Second, 1<<20, func(options *ServerOptions) {
		options.IdempotencyRedis = redisClient
		for _, opt := range opts {
			opt(options)
		}
	})
	gatewayServer := httptest.NewServer(authenticatedTestHandler(server))
	t.Cleanup(gatewayServer.Close)
	return gatewayServer.URL
}

// countingCreates answers each create with a new sandbox ID after delay.
func countingCreates(calls *atomic.Int32, delay time.Duration, firstStatus int) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		call := calls.Add(1)
		time.Sleep(delay)
		if call == 1 && firstStatus != http.StatusCreated {
			http.Error(w, "create failed", firstStatus)
			return
		}
		id := "sbx-" + strconv.Itoa(int(call))
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set(headerSandboxID, id)
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"sandboxID":"`+id+`"}`)
	})
}

func doCreate(gatewayURL, key, body string) (int, string, http.Header, error) {
	req, err := http.NewRequest(http.MethodPost, gatewayURL+"/sandboxes-cold", bytes.NewBufferString(body))
	if err != nil {
		return 0, "", nil, err
	}
	req.Header.Set(headerIdempotencyKey, key)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, "", nil, err
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(resp.Body)
	return resp.StatusCode, string(payload), resp.Header, err
}

func postCreate(t *testing.T, gatewayURL, key, body string) (int, string, http.Header) {
	t.Helper()
	status, payload, headers, err := doCreate(gatewayURL, key, body)
	if err != nil {
		t.Fatalf("create request failed: %v", err)
	}
	return status, payload, headers
}

func TestIdempotentCreateReplaysFirstResult(t *testing.T) {
	var calls atomic.Int32
	gatewayURL := newIdempotencyGateway(t, countingCreates(&calls, 0, http.StatusCreated))

	status, first, _ := postCreate(t, gatewayURL, "key-1", `{"image":"ubuntu:22.04"}`)
	if status != http.StatusCreated {
		t.Fatalf("first status = %d, want 201", status)
	}
	status, second, headers := postCreate(t, gatewayURL, "key-1", `{"image":"ubuntu:22.04"}`)
	if status != http.StatusCreated || second != first || headers.Get(headerIdempotentReplayed) != "true" {
		t.Fatalf("retry = %d %q replayed=%q, want 201 %q replayed=true", status, second, headers.Get(headerIdempotentReplayed), first)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("upstream creates = %d, want 1", got)
	}
}

func TestIdempotentCreateRejectsKeyReuseWithDifferentBody(t *testing.T) {
	var calls atomic.Int32
	gatewayURL := newIdempotencyGateway(t, countingCreates(&calls, 0, http.StatusCreated))

	postCreate(t, gatewayURL, "key-1", `{"image":"ubuntu:22.04"}`)
	if status, _, _ := postCreate(t, gatewayURL, "key-1", `{"image":"alpine:3"}`); status != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", status)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("upstream creates = %d, want 1", got)
	}
}

func TestIdempotentCreateReleasesKeyAfterFailure(t *testing.T) {
	var calls atomic.Int32
	gatewayURL := newIdempotencyGateway(t, countingCreates(&calls, 0, http.StatusInternalServerError))

	if status, _, _ := postCreate(t, gatewayURL, "key-1", `{}`); status != http.StatusInternalServerError {
		t.Fatalf("first status = %d, want 500", status)
	}
	status, _, headers := postCreate(t, gatewayURL, "key-1", `{}`)
	if status != http.StatusCreated || headers.Get(headerIdempotentReplayed) != "" {
		t.Fatalf("retry = %d replayed=%q, want a fresh 201", status, headers.Get(headerIdempotentReplayed))
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("upstream creates = %d, want 2", got)
	}
}

func TestIdempotentCreateKeepsReplyForConfiguredTTL(t *testing.T) {
	var calls atomic.Int32
	var redisClient *redis.Client
	gatewayURL := newIdempotencyGateway(t, countingCreates(&calls, 0, http.StatusCreated), func(options *ServerOptions) {
		options.IdempotencyKeyTTL = 2 * time.Hour
		redisClient = options.IdempotencyRedis
	})

	postCreate(t, gatewayURL, "key-1", `{}`)
	if ttl := redisClient.PTTL(context.Background(), idempotencyKeyPrefix+"key-1").Val(); ttl <= time.Hour || ttl > 2*time.Hour {
		t.Fatalf("saved reply ttl = %s, want about 2h", ttl)
	}
}

func TestConcurrentIdempotentCreatesReachUpstreamOnce(t *testing.T) {
	var calls atomic.Int32
	gatewayURL := newIdempotencyGateway(t, countingCreates(&calls, 300*time.Millisecond, http.StatusCreated))

	bodies := make([]string, 5)
	var wg sync.WaitGroup
	for i := range bodies {
		wg.Add(1)
		go func() {
			defer wg.Done()
			status, body, _, err := doCreate(gatewayURL, "key-1", `{}`)
			if err != nil || status != http.StatusCreated {
				t.Errorf("create = %d, %v; want 201", status, err)
			}
			bodies[i] = body
		}()
	}
	wg.Wait()
	for _, body := range bodies {
		if body != bodies[0] {
			t.Fatalf("responses differ: %q vs %q", body, bodies[0])
		}
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("upstream creates = %d, want 1", got)
	}
}
