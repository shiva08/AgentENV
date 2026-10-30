// Measures how many creates per second one scheduler leader can place.
//
// Run from the repository root:
//
//	cp docs/proofs/07-admission-control/scripts/leader_throughput_bench_test.go services/scheduler/internal/zz_leader_throughput_bench_test.go
//	(cd services && go test ./scheduler/internal/ -run '^$' -bench BenchmarkLeaderCreates -benchtime 2s)
//	rm services/scheduler/internal/zz_leader_throughput_bench_test.go
//
// It calls the scheduler in process (no network), from as many goroutines as CPUs.
// The "place+redis" case starts its own redis-server and also records each binding.
package scheduler

import (
	"context"
	"fmt"
	"net"
	"os/exec"
	"sync/atomic"
	"testing"
	"time"

	schedulerv1 "agentenv/services/api/proto"
	"agentenv/services/shared/config"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
)

func benchService(nodes, inFlight int, store BindingStore) *Service {
	list := make([]Node, nodes)
	for i := range list {
		list[i] = Node{ID: fmt.Sprintf("node-%d", i), Endpoint: fmt.Sprintf("http://node-%d", i)}
	}
	registry := NewAtomicNodeRegistry(list, defaultObservedReportTTL)
	maxSandboxes := uint32(1 << 30)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), store,
		WithNodeResourceLimit(&config.NodeResourceLimit{MaxSandboxCount: &maxSandboxes}))
	now := time.Now()
	for _, n := range list {
		heartbeatNode(registry, n.ID, n.Endpoint, now)
	}
	// Reservations still waiting for a heartbeat: about 5 s of creates.
	for i := 0; i < inFlight; i++ {
		service.reserved.addLocked(list[i%nodes].ID, usage{sandboxes: 1}, now)
	}
	return service
}

func benchRedis(b *testing.B) string {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		b.Fatal(err)
	}
	_, port, _ := net.SplitHostPort(listener.Addr().String())
	_ = listener.Close()
	cmd := exec.Command("redis-server", "--bind", "127.0.0.1", "--port", port, "--save", "", "--appendonly", "no", "--dir", b.TempDir(), "--loglevel", "warning")
	if err := cmd.Start(); err != nil {
		b.Fatal(err)
	}
	b.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
	addr := net.JoinHostPort("127.0.0.1", port)
	client := redis.NewClient(&redis.Options{Addr: addr})
	defer client.Close()
	for i := 0; i < 200; i++ {
		if client.Ping(context.Background()).Err() == nil {
			return addr
		}
		time.Sleep(25 * time.Millisecond)
	}
	b.Fatal("redis-server did not start")
	return ""
}

func runCreates(b *testing.B, service *Service, record bool) {
	var seq, failures atomic.Int64
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			resp, err := service.Schedule(context.Background(), coldCreate())
			if err != nil {
				failures.Add(1)
				continue
			}
			if record {
				if _, err := service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
					SandboxId:     fmt.Sprintf("sbx-%d", seq.Add(1)),
					Node:          resp.GetNode(),
					ReservationId: resp.GetReservationId(),
				}); err != nil {
					failures.Add(1)
				}
			}
			service.reserved.release(resp.GetReservationId())
		}
	})
	b.StopTimer()
	if failures.Load() > 0 {
		b.Fatalf("%d creates failed", failures.Load())
	}
	b.ReportMetric(float64(b.N)/b.Elapsed().Seconds(), "creates/s")
}

func BenchmarkLeaderCreates(b *testing.B) {
	for _, nodes := range []int{100, 1000, 5000} {
		b.Run(fmt.Sprintf("place/nodes=%d", nodes), func(b *testing.B) {
			runCreates(b, benchService(nodes, 5*nodes, NewInMemoryBindingStore(defaultObservedReportTTL)), false)
		})
		b.Run(fmt.Sprintf("place-no-inflight/nodes=%d", nodes), func(b *testing.B) {
			runCreates(b, benchService(nodes, 0, NewInMemoryBindingStore(defaultObservedReportTTL)), false)
		})
		b.Run(fmt.Sprintf("no-limits/nodes=%d", nodes), func(b *testing.B) {
			service := benchService(nodes, 0, NewInMemoryBindingStore(defaultObservedReportTTL))
			service.resourceLimit = nil
			runCreates(b, service, false)
		})
	}
	addr := benchRedis(b)
	b.Run("place+redis/nodes=1000", func(b *testing.B) {
		store, err := NewRedisBindingStore(addr, time.Hour)
		if err != nil {
			b.Fatal(err)
		}
		defer store.Close()
		runCreates(b, benchService(1000, 5000, store), true)
	})
}
