package scheduler

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	schedulerv1 "agentenv/services/api/proto"
	"agentenv/services/shared/config"

	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type failingBindingStore struct{}

func (failingBindingStore) Get(string, time.Time) (Node, bool, error) {
	return Node{}, false, errors.New("binding store failed")
}

func (failingBindingStore) Record(string, Node, time.Time) error {
	return errors.New("binding store failed")
}

func (failingBindingStore) ReconcileNode(Node, []string, time.Time) error {
	return errors.New("binding store failed")
}

func registerObservedNodeForTest(t *testing.T, service *Service, nodeID string, serviceInstanceID string) {
	t.Helper()
	_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
		NodeId:            nodeID,
		ClusterId:         "cluster-1",
		ServiceInstanceId: serviceInstanceID,
		Version:           "0.1.0",
		Commit:            "abc123",
		Snapshot: &schedulerv1.NodeSnapshot{
			Status:               schedulerv1.NodeStatus_NODE_STATUS_READY,
			SandboxCount:         2,
			CreateSuccesses:      5,
			ReportedAtUnixMs:     123,
			AllocatedCpu:         2,
			MemoryUsedBytes:      128,
			MemoryTotalBytes:     256,
			AllocatedMemoryBytes: 512,
		},
	})
	if err != nil {
		t.Fatalf("heartbeat failed: %v", err)
	}
}

func TestLookupNodeReturnsRecordedAssignment(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL)
	service := NewService(
		zap.NewNop(),
		registry,
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId: "sbx-1",
		Node:      (&Node{ID: "node-a", Endpoint: "http://node-a"}).ToProto(),
	})
	if err != nil {
		t.Fatalf("record assignment failed: %v", err)
	}

	resp, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"})
	if err != nil {
		t.Fatalf("lookup failed: %v", err)
	}
	if got := resp.GetNode().GetNodeId(); got != "node-a" {
		t.Fatalf("expected node-a, got %s", got)
	}
}

func TestLookupNodeReturnsTheNodesCurrentAddress(t *testing.T) {
	store := NewInMemoryBindingStore(defaultObservedReportTTL)
	store.Record("sbx-1", Node{ID: "worker-1", Endpoint: "http://10.0.0.1:8000"}, time.Now())
	registry := NewAtomicNodeRegistry([]Node{{ID: "worker-1", Endpoint: "http://10.0.0.9:8000"}}, defaultObservedReportTTL)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), store)

	resp, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"})
	if err != nil {
		t.Fatalf("lookup failed: %v", err)
	}
	if got := resp.GetNode().GetEndpoint(); got != "http://10.0.0.9:8000" {
		t.Fatalf("expected the node's current address, got %q", got)
	}
}

func TestListNodesReturnsActiveAndLingeringNodes(t *testing.T) {
	registry := NewAtomicNodeRegistry(nil, defaultObservedReportTTL)
	// node-a: active (included)
	// node-b: lingering (included)
	registry.Set(
		[]Node{{ID: "node-a", Endpoint: "http://node-a"}},
		[]Node{{ID: "node-b", Endpoint: "http://node-b"}},
	)

	service := NewService(
		zap.NewNop(),
		registry,
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	resp, err := service.ListNodes(context.Background(), &schedulerv1.ListNodesRequest{})
	if err != nil {
		t.Fatalf("list nodes failed: %v", err)
	}

	if len(resp.GetNodes()) != 2 {
		t.Fatalf("expected 2 nodes (active+lingering), got %d", len(resp.GetNodes()))
	}
	ids := map[string]bool{}
	for _, n := range resp.GetNodes() {
		ids[n.GetNodeId()] = true
	}
	if !ids["node-a"] || !ids["node-b"] {
		t.Fatalf("expected node-a and node-b, got %v", ids)
	}
}

func TestQueryOnlyServiceOnlySupportsLookupNode(t *testing.T) {
	store := NewInMemoryBindingStore(defaultObservedReportTTL)
	store.Record("sbx-1", Node{ID: "node-a", Endpoint: "http://node-a"}, time.Now())
	service := NewQueryOnlyService(zap.NewNop(), store)

	resp, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"})
	if err != nil {
		t.Fatalf("query-only lookup failed: %v", err)
	}
	if got := resp.GetNode().GetNodeId(); got != "node-a" {
		t.Fatalf("expected node-a, got %q", got)
	}

	_, err = service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{})
	if status.Code(err) != codes.Unimplemented {
		t.Fatalf("expected unsupported Schedule to return unimplemented, got %v", err)
	}
}

func TestLookupNodeReturnsUnavailableWhenBindingStoreFails(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		failingBindingStore{},
	)

	_, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected unavailable, got %v", err)
	}
}

func TestLookupNodeReturnsNotFoundWhenAssignmentMissing(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "missing"})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("expected not found, got %v", err)
	}
}

func TestRecordAssignmentReturnsUnavailableWhenBindingStoreFails(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		failingBindingStore{},
	)

	_, err := service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId: "sbx-1",
		Node:      (&Node{ID: "node-a", Endpoint: "http://node-a"}).ToProto(),
	})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected unavailable, got %v", err)
	}
}

func TestRecordAssignmentRejectsUnknownNode(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId: "sbx-1",
		Node:      (&Node{ID: "node-x", Endpoint: "http://node-x"}).ToProto(),
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("expected invalid argument, got %v", err)
	}
}

func TestListNodesReflectsRegistryUpdates(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL)
	service := NewService(
		zap.NewNop(),
		registry,
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	registerObservedNodeForTest(t, service, "node-a", "svc-a")

	// Replace discovery with node-b; node-a is removed from discovery.
	registry.Set([]Node{{ID: "node-b", Endpoint: "http://node-b"}}, nil)
	registerObservedNodeForTest(t, service, "node-b", "svc-b")

	resp, err := service.ListNodes(context.Background(), &schedulerv1.ListNodesRequest{})
	if err != nil {
		t.Fatalf("list nodes failed: %v", err)
	}
	if len(resp.GetNodes()) != 1 {
		t.Fatalf("expected 1 node, got %d", len(resp.GetNodes()))
	}
	if got := resp.GetNodes()[0].GetNodeId(); got != "node-b" {
		t.Fatalf("expected node-b, got %q", got)
	}
}

func TestScheduleReturnsUnavailableWhenRegistryIsEmpty(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry(nil, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{Hint: &schedulerv1.ScheduleRequestHint{Kind: &schedulerv1.ScheduleRequestHint_NewSandbox{NewSandbox: &schedulerv1.NewSandboxHint{}}}})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected unavailable, got %v", err)
	}
}

func TestScheduleOnlyConsidersReadyNodes(t *testing.T) {
	registry := NewAtomicNodeRegistry(nil, defaultObservedReportTTL)
	// node-a: active, node-b: lingering
	registry.Set(
		[]Node{{ID: "node-a", Endpoint: "http://node-a"}},
		[]Node{{ID: "node-b", Endpoint: "http://node-b"}},
	)
	service := NewService(
		zap.NewNop(),
		registry,
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	// Heartbeat both
	registerObservedNodeForTest(t, service, "node-a", "svc-a")
	registerObservedNodeForTest(t, service, "node-b", "svc-b")

	// Schedule should only pick node-a (ready), never node-b (no_schedule)
	for i := 0; i < 5; i++ {
		resp, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{})
		if err != nil {
			t.Fatalf("schedule failed: %v", err)
		}
		if got := resp.GetNode().GetNodeId(); got != "node-a" {
			t.Fatalf("iteration %d: expected node-a, got %s", i, got)
		}
	}
}

func TestScheduleSkipsNodesWithoutFreshHeartbeat(t *testing.T) {
	cases := []struct {
		name       string
		setupNodeB func(t *testing.T, registry *AtomicNodeRegistry)
	}{
		{
			name: "stale heartbeat",
			setupNodeB: func(_ *testing.T, registry *AtomicNodeRegistry) {
				heartbeatNode(registry, "node-b", "http://node-b", time.Now().Add(-2*defaultObservedReportTTL))
			},
		},
		{
			name: "unregistered",
			setupNodeB: func(t *testing.T, registry *AtomicNodeRegistry) {
				heartbeatNode(registry, "node-b", "http://node-b", time.Now())
				if err := registry.UnregisterObserved("node-b", "svc-node-b"); err != nil {
					t.Fatalf("unregister failed: %v", err)
				}
			},
		},
		{
			name:       "never reported",
			setupNodeB: func(*testing.T, *AtomicNodeRegistry) {},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			registry := NewAtomicNodeRegistry([]Node{
				{ID: "node-a", Endpoint: "http://node-a"},
				{ID: "node-b", Endpoint: "http://node-b"},
			}, defaultObservedReportTTL)
			service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), NewInMemoryBindingStore(defaultObservedReportTTL))
			heartbeatNode(registry, "node-a", "http://node-a", time.Now())
			tc.setupNodeB(t, registry)

			for i := 0; i < 4; i++ {
				resp, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{})
				if err != nil {
					t.Fatalf("schedule failed: %v", err)
				}
				if got := resp.GetNode().GetNodeId(); got != "node-a" {
					t.Fatalf("schedule %d: expected node-a, got %s", i, got)
				}
			}
		})
	}
}

func TestScheduleFailsWhenNoNodeIsReady(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{
		{ID: "node-a", Endpoint: "http://node-a"},
		{ID: "node-b", Endpoint: "http://node-b"},
	}, defaultObservedReportTTL)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), NewInMemoryBindingStore(defaultObservedReportTTL))
	// node-a's heartbeat is stale and node-b never reported.
	heartbeatNode(registry, "node-a", "http://node-a", time.Now().Add(-2*defaultObservedReportTTL))

	_, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected unavailable, got %v", err)
	}
}

func TestScheduleSkipsExcludedNodes(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{
		{ID: "node-a", Endpoint: "http://node-a"},
		{ID: "node-b", Endpoint: "http://node-b"},
	}, defaultObservedReportTTL)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), NewInMemoryBindingStore(defaultObservedReportTTL))
	heartbeatNode(registry, "node-a", "http://node-a", time.Now())
	heartbeatNode(registry, "node-b", "http://node-b", time.Now())

	for i := 0; i < 4; i++ {
		resp, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{ExcludeNodeIds: []string{"node-a"}})
		if err != nil {
			t.Fatalf("schedule failed: %v", err)
		}
		if got := resp.GetNode().GetNodeId(); got != "node-b" {
			t.Fatalf("schedule %d: expected node-b, got %s", i, got)
		}
	}

	_, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{ExcludeNodeIds: []string{"node-a", "node-b"}})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected unavailable when every node is excluded, got %v", err)
	}
}

func TestScheduleUsesUnreportedNodesDuringStartupGrace(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{
		{ID: "node-a", Endpoint: "http://node-a"},
		{ID: "node-b", Endpoint: "http://node-b"},
	}, defaultObservedReportTTL)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), NewInMemoryBindingStore(defaultObservedReportTTL),
		WithStartupGrace(defaultObservedReportTTL))
	heartbeatNode(registry, "node-a", "http://node-a", time.Now())

	picked := map[string]int{}
	for i := 0; i < 4; i++ {
		resp, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{})
		if err != nil {
			t.Fatalf("schedule failed: %v", err)
		}
		picked[resp.GetNode().GetNodeId()]++
	}
	if picked["node-a"] != 2 || picked["node-b"] != 2 {
		t.Fatalf("during the grace period, expected both nodes, got %v", picked)
	}

	service.startedAt = time.Now().Add(-2 * defaultObservedReportTTL)
	for i := 0; i < 4; i++ {
		resp, err := service.Schedule(context.Background(), &schedulerv1.ScheduleRequest{})
		if err != nil {
			t.Fatalf("schedule failed: %v", err)
		}
		if got := resp.GetNode().GetNodeId(); got != "node-a" {
			t.Fatalf("after the grace period, schedule %d: expected node-a, got %s", i, got)
		}
	}
}

func coldCreate() *schedulerv1.ScheduleRequest {
	return &schedulerv1.ScheduleRequest{Hint: &schedulerv1.ScheduleRequestHint{
		Kind: &schedulerv1.ScheduleRequestHint_NewColdSandbox{NewColdSandbox: &schedulerv1.NewColdSandboxHint{CpuCount: 1, MemoryMb: 512}},
	}}
}

// limitedService has two ready, empty nodes that each take at most maxSandboxes.
func limitedService(maxSandboxes uint32) *Service {
	registry := NewAtomicNodeRegistry([]Node{
		{ID: "node-a", Endpoint: "http://node-a"},
		{ID: "node-b", Endpoint: "http://node-b"},
	}, defaultObservedReportTTL)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), NewInMemoryBindingStore(defaultObservedReportTTL),
		WithNodeResourceLimit(&config.NodeResourceLimit{MaxSandboxCount: &maxSandboxes}))
	heartbeatNode(registry, "node-a", "http://node-a", time.Now())
	heartbeatNode(registry, "node-b", "http://node-b", time.Now())
	return service
}

func expectNoCapacity(t *testing.T, err error) {
	t.Helper()
	if status.Code(err) != codes.Unavailable || status.Convert(err).Message() != "no node has capacity" {
		t.Fatalf("expected unavailable, no node has capacity, got %v", err)
	}
}

func TestScheduleReservesCapacityForCreatesArrivingTogether(t *testing.T) {
	service := limitedService(1)
	const creates = 20
	var wg sync.WaitGroup
	var mu sync.Mutex
	placed := map[string]int{}
	refused := 0
	for i := 0; i < creates; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := service.Schedule(context.Background(), coldCreate())
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				refused++
				return
			}
			if resp.GetReservationId() == "" {
				t.Errorf("create placed on %s without a reservation", resp.GetNode().GetNodeId())
			}
			placed[resp.GetNode().GetNodeId()]++
		}()
	}
	wg.Wait()
	if placed["node-a"] != 1 || placed["node-b"] != 1 || refused != creates-2 {
		t.Fatalf("expected one create per node and %d refused, got placed %v, refused %d", creates-2, placed, refused)
	}
	_, err := service.Schedule(context.Background(), coldCreate())
	expectNoCapacity(t, err)
}

func TestReleaseReservationFreesTheSlot(t *testing.T) {
	service := limitedService(1)
	first, err := service.Schedule(context.Background(), coldCreate())
	if err != nil {
		t.Fatalf("schedule failed: %v", err)
	}
	if _, err := service.Schedule(context.Background(), coldCreate()); err != nil {
		t.Fatalf("schedule failed: %v", err)
	}
	_, err = service.Schedule(context.Background(), coldCreate())
	expectNoCapacity(t, err)

	if _, err := service.ReleaseReservation(context.Background(), &schedulerv1.ReleaseReservationRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("expected invalid argument without a reservation ID, got %v", err)
	}
	if _, err := service.ReleaseReservation(context.Background(), &schedulerv1.ReleaseReservationRequest{ReservationId: first.GetReservationId()}); err != nil {
		t.Fatalf("release failed: %v", err)
	}
	resp, err := service.Schedule(context.Background(), coldCreate())
	if err != nil {
		t.Fatalf("schedule after release failed: %v", err)
	}
	if got, want := resp.GetNode().GetNodeId(), first.GetNode().GetNodeId(); got != want {
		t.Fatalf("expected the released slot on %s, got %s", want, got)
	}
}

// oneNodeService has one ready, empty node that takes at most maxSandboxes, and a heartbeat that reports
// running sandboxes and lists sandboxIDs for it.
func oneNodeService(t *testing.T, maxSandboxes uint32) (*Service, func(running uint32, sandboxIDs ...string)) {
	registry := NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), NewInMemoryBindingStore(defaultObservedReportTTL),
		WithNodeResourceLimit(&config.NodeResourceLimit{MaxSandboxCount: &maxSandboxes}))
	heartbeat := func(running uint32, sandboxIDs ...string) {
		t.Helper()
		_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
			NodeId:            "node-a",
			ServiceInstanceId: "svc-node-a",
			Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY, SandboxCount: running},
			SandboxIds:        sandboxIDs,
		})
		if err != nil {
			t.Fatalf("heartbeat failed: %v", err)
		}
	}
	heartbeat(0)
	return service, heartbeat
}

// scheduleAndRecord places a create and records that it made sandboxID.
func scheduleAndRecord(t *testing.T, service *Service, sandboxID string) {
	t.Helper()
	resp, err := service.Schedule(context.Background(), coldCreate())
	if err != nil {
		t.Fatalf("schedule failed: %v", err)
	}
	_, err = service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId:     sandboxID,
		Node:          resp.GetNode(),
		ReservationId: resp.GetReservationId(),
	})
	if err != nil {
		t.Fatalf("record assignment failed: %v", err)
	}
}

func TestHeartbeatListingTheSandboxEndsItsReservation(t *testing.T) {
	service, heartbeat := oneNodeService(t, 2)
	scheduleAndRecord(t, service, "sbx-1")
	// The node now counts sbx-1 itself; counting its reservation too would leave no room.
	heartbeat(1, "sbx-1")
	if _, err := service.Schedule(context.Background(), coldCreate()); err != nil {
		t.Fatalf("1 sandbox plus this create fits a limit of 2, got %v", err)
	}
	_, err := service.Schedule(context.Background(), coldCreate())
	expectNoCapacity(t, err)
}

func TestReservationEndsWhenAHeartbeatListedTheSandboxBeforeItsAssignment(t *testing.T) {
	service, heartbeat := oneNodeService(t, 1)
	resp, err := service.Schedule(context.Background(), coldCreate())
	if err != nil {
		t.Fatalf("schedule failed: %v", err)
	}
	// The node lists sbx-1 while creating it, before the gateway records the assignment.
	heartbeat(1, "sbx-1")
	_, err = service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId:     "sbx-1",
		Node:          resp.GetNode(),
		ReservationId: resp.GetReservationId(),
	})
	if err != nil {
		t.Fatalf("record assignment failed: %v", err)
	}
	// sbx-1 is deleted before the next heartbeat.
	heartbeat(0)
	if _, err := service.Schedule(context.Background(), coldCreate()); err != nil {
		t.Fatalf("the node is empty, got %v", err)
	}
}

func TestReservationEndsWhenTheSandboxIsGoneBeforeAnyHeartbeatListsIt(t *testing.T) {
	service, heartbeat := oneNodeService(t, 1)
	scheduleAndRecord(t, service, "sbx-1")
	// A heartbeat built before sbx-1 existed may arrive after the assignment, so one miss is not enough.
	heartbeat(0)
	_, err := service.Schedule(context.Background(), coldCreate())
	expectNoCapacity(t, err)
	heartbeat(0)
	if _, err := service.Schedule(context.Background(), coldCreate()); err != nil {
		t.Fatalf("two heartbeats without sbx-1 mean it is gone, got %v", err)
	}
}

func TestReservationsExpire(t *testing.T) {
	saved := reservationTTL
	reservationTTL = -time.Second
	defer func() { reservationTTL = saved }()
	service := limitedService(1)
	for i := 0; i < 4; i++ {
		if _, err := service.Schedule(context.Background(), coldCreate()); err != nil {
			t.Fatalf("schedule %d failed although every earlier reservation expired: %v", i, err)
		}
	}
}

func TestScheduleReservesOnlyForCreatesWithLimits(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL)
	unlimited := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), NewInMemoryBindingStore(defaultObservedReportTTL))
	heartbeatNode(registry, "node-a", "http://node-a", time.Now())
	resp, err := unlimited.Schedule(context.Background(), coldCreate())
	if err != nil || resp.GetReservationId() != "" {
		t.Fatalf("without limits, expected no reservation, got %q, %v", resp.GetReservationId(), err)
	}

	limited := limitedService(1)
	for i := 0; i < 4; i++ {
		resp, err := limited.Schedule(context.Background(), &schedulerv1.ScheduleRequest{})
		if err != nil || resp.GetReservationId() != "" {
			t.Fatalf("a request that creates no sandbox should reserve nothing, got %q, %v", resp.GetReservationId(), err)
		}
	}
}

func TestGetObservedNodeAfterHeartbeat(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	registerObservedNodeForTest(t, service, "node-a", "svc-a")

	resolved, err := service.GetNode(context.Background(), &schedulerv1.GetNodeRequest{
		NodeId:    "node-a",
		ClusterId: "cluster-1",
	})
	if err != nil {
		t.Fatalf("resolve observed node failed: %v", err)
	}
	if resolved.GetNode().GetNodeId() != "node-a" {
		t.Fatalf("unexpected resolved node id: %s", resolved.GetNode().GetNodeId())
	}
	if resolved.GetNode().GetSnapshot().GetStatus() != schedulerv1.NodeStatus_NODE_STATUS_READY {
		t.Fatalf("unexpected resolved status: %v", resolved.GetNode().GetSnapshot().GetStatus())
	}
	if resolved.GetNode().GetSnapshot().GetSandboxCount() != 2 {
		t.Fatalf("unexpected sandbox count: %d", resolved.GetNode().GetSnapshot().GetSandboxCount())
	}
}

func TestHeartbeatRebuildsSandboxBindings(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
		NodeId:            "node-a",
		ClusterId:         "cluster-1",
		ServiceInstanceId: "svc-a",
		SandboxIds:        []string{"sbx-1", "sbx-2"},
		Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
	})
	if err != nil {
		t.Fatalf("heartbeat failed: %v", err)
	}

	for _, sandboxID := range []string{"sbx-1", "sbx-2"} {
		resp, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: sandboxID})
		if err != nil {
			t.Fatalf("lookup %s failed: %v", sandboxID, err)
		}
		if got := resp.GetNode().GetNodeId(); got != "node-a" {
			t.Fatalf("lookup %s returned node %q, want %q", sandboxID, got, "node-a")
		}
	}
}

func TestHeartbeatRemovesBindingsMissingFromRoster(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId: "sbx-1",
		Node:      (&Node{ID: "node-a", Endpoint: "http://node-a"}).ToProto(),
	})
	if err != nil {
		t.Fatalf("record assignment failed: %v", err)
	}
	_, err = service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId: "sbx-2",
		Node:      (&Node{ID: "node-a", Endpoint: "http://node-a"}).ToProto(),
	})
	if err != nil {
		t.Fatalf("record assignment failed: %v", err)
	}

	_, err = service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
		NodeId:            "node-a",
		ClusterId:         "cluster-1",
		ServiceInstanceId: "svc-a",
		SandboxIds:        []string{"sbx-1"},
		Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
	})
	if err != nil {
		t.Fatalf("heartbeat failed: %v", err)
	}

	if _, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"}); err != nil {
		t.Fatalf("expected sbx-1 to still be bound, got %v", err)
	}
	if _, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-2"}); status.Code(err) != codes.NotFound {
		t.Fatalf("expected missing binding to be removed, got %v", err)
	}
}

// countingBindingStore counts ReconcileNode calls.
type countingBindingStore struct {
	*InMemoryBindingStore
	reconciles int
}

func (s *countingBindingStore) ReconcileNode(node Node, sandboxIDs []string, now time.Time) error {
	s.reconciles++
	return s.InMemoryBindingStore.ReconcileNode(node, sandboxIDs, now)
}

func TestHeartbeatSkipsUnchangedSandboxList(t *testing.T) {
	store := &countingBindingStore{InMemoryBindingStore: NewInMemoryBindingStore(defaultObservedReportTTL)}
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		store,
	)
	heartbeat := func(sandboxIDs ...string) {
		t.Helper()
		if _, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
			NodeId:            "node-a",
			ServiceInstanceId: "svc-a",
			SandboxIds:        sandboxIDs,
			Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
		}); err != nil {
			t.Fatalf("heartbeat failed: %v", err)
		}
	}

	heartbeat("sbx-1", "sbx-2")
	heartbeat("sbx-2", "sbx-1")
	if store.reconciles != 1 {
		t.Fatalf("expected the same list in another order to be skipped, got %d reconciles", store.reconciles)
	}
	heartbeat("sbx-1")
	if store.reconciles != 2 {
		t.Fatalf("expected a changed list to be applied, got %d reconciles", store.reconciles)
	}

	defer func(saved time.Duration) { sandboxListResync = saved }(sandboxListResync)
	sandboxListResync = 0
	heartbeat("sbx-1")
	if store.reconciles != 3 {
		t.Fatalf("expected an unchanged list to be applied again after the resync interval, got %d reconciles", store.reconciles)
	}
}

func TestHeartbeatRewritesBindingsRedisLost(t *testing.T) {
	defer func(saved time.Duration) { storeLossCheckInterval = saved }(storeLossCheckInterval)
	storeLossCheckInterval = 0
	store := newRedisBindingStoreForTest(t, time.Hour)
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		store,
	)
	heartbeat := func(sandboxIDs ...string) {
		t.Helper()
		if _, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
			NodeId:            "node-a",
			ServiceInstanceId: "svc-a",
			SandboxIds:        sandboxIDs,
			Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
		}); err != nil {
			t.Fatalf("heartbeat failed: %v", err)
		}
	}
	lookup := func() codes.Code {
		_, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"})
		return status.Code(err)
	}

	heartbeat("sbx-1")
	if code := lookup(); code != codes.OK {
		t.Fatalf("expected the heartbeat to bind sbx-1, got %v", code)
	}
	// Redis restarting without persistence, or failing over to an empty replica, loses every key.
	if err := store.client.FlushAll(context.Background()).Err(); err != nil {
		t.Fatalf("flush redis failed: %v", err)
	}
	if code := lookup(); code != codes.NotFound {
		t.Fatalf("expected the flush to drop the binding, got %v", code)
	}
	heartbeat("sbx-1")
	if code := lookup(); code != codes.OK {
		t.Fatalf("expected the next heartbeat to rewrite the binding Redis lost, got %v", code)
	}
}

// flakyBindingStore fails ReconcileNode while down is set.
type flakyBindingStore struct {
	*InMemoryBindingStore
	down bool
}

func (s *flakyBindingStore) ReconcileNode(node Node, sandboxIDs []string, now time.Time) error {
	if s.down {
		return errors.New("binding store failed")
	}
	return s.InMemoryBindingStore.ReconcileNode(node, sandboxIDs, now)
}

func TestHeartbeatSucceedsWhileTheBindingStoreIsDown(t *testing.T) {
	store := &flakyBindingStore{InMemoryBindingStore: NewInMemoryBindingStore(defaultObservedReportTTL), down: true}
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		store,
	)
	cpuConfig := buildConfig(nil, []cpuidModifier{{
		Leaf: "0x1", Subleaf: "0x0",
		Modifiers: []registerMod{{Register: "eax", Bitmap: bm32(0xFF)}},
	}}, nil)
	heartbeat := func() *schedulerv1.HeartbeatResponse {
		t.Helper()
		resp, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
			NodeId:            "node-a",
			ClusterId:         "cluster-1",
			ServiceInstanceId: "svc-a",
			SandboxIds:        []string{"sbx-1"},
			MachineInfo:       &schedulerv1.MachineInfo{CpuConfigJson: cpuConfig},
			Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
		})
		if err != nil {
			t.Fatalf("a failed binding write must not fail the heartbeat, got %v", err)
		}
		return resp
	}

	if resp := heartbeat(); resp.GetCpuConfigJson() == "" {
		t.Fatal("expected the shared CPU features in the reply although the binding write failed")
	}
	store.down = false
	heartbeat()
	if _, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"}); err != nil {
		t.Fatalf("expected the next heartbeat to retry the binding write, got %v", err)
	}
}

func TestUnregisterObservedNodeIsIdempotent(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	registerObservedNodeForTest(t, service, "node-a", "svc-a")

	_, err := service.UnregisterNode(context.Background(), &schedulerv1.UnregisterNodeRequest{
		NodeId:            "node-a",
		ServiceInstanceId: "svc-a",
	})
	if err != nil {
		t.Fatalf("unregister failed: %v", err)
	}

	_, err = service.UnregisterNode(context.Background(), &schedulerv1.UnregisterNodeRequest{
		NodeId:            "node-a",
		ServiceInstanceId: "svc-a",
	})
	if err != nil {
		t.Fatalf("second unregister should be idempotent, got: %v", err)
	}
}

func TestUnregisterNodeKeepsBindings(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	registerObservedNodeForTest(t, service, "node-a", "svc-a")

	_, err := service.RecordAssignment(context.Background(), &schedulerv1.RecordAssignmentRequest{
		SandboxId: "sbx-1",
		Node:      (&Node{ID: "node-a", Endpoint: "http://node-a"}).ToProto(),
	})
	if err != nil {
		t.Fatalf("record assignment failed: %v", err)
	}

	_, err = service.UnregisterNode(context.Background(), &schedulerv1.UnregisterNodeRequest{
		NodeId:            "node-a",
		ServiceInstanceId: "svc-a",
	})
	if err != nil {
		t.Fatalf("unregister failed: %v", err)
	}

	resp, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"})
	if err != nil || resp.GetNode().GetNodeId() != "node-a" {
		t.Fatalf("expected the binding to survive unregister, got (%v, %v)", resp, err)
	}
}

func TestLookupNodeReportsUnknownNodeUnavailable(t *testing.T) {
	store := NewInMemoryBindingStore(defaultObservedReportTTL)
	store.Record("sbx-1", Node{ID: "worker-gone", Endpoint: "http://10.0.0.1:8000"}, time.Now())
	registry := NewAtomicNodeRegistry([]Node{{ID: "worker-1", Endpoint: "http://10.0.0.9:8000"}}, defaultObservedReportTTL)
	service := NewService(zap.NewNop(), registry, NewStrategy("round_robin"), store)

	_, err := service.LookupNode(context.Background(), &schedulerv1.LookupNodeRequest{SandboxId: "sbx-1"})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected unavailable instead of the saved address, got %v", err)
	}
}

func TestP2pArtifactLookupReturnsReadyIndexedProviders(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{
		{ID: "node-a", Endpoint: "http://node-a"},
		{ID: "node-b", Endpoint: "http://node-b"},
		{ID: "node-c", Endpoint: "http://node-c"},
	}, defaultObservedReportTTL)
	service := NewService(
		zap.NewNop(),
		registry,
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	for _, nodeID := range []string{"node-a", "node-b", "node-c"} {
		_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
			NodeId:            nodeID,
			ClusterId:         "cluster-1",
			ServiceInstanceId: "svc-" + nodeID,
			Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
			P2PEndpoint: &schedulerv1.P2PEndpoint{
				Backend: "backend",
				Address: "addr-" + nodeID,
			},
		})
		if err != nil {
			t.Fatalf("heartbeat %s failed: %v", nodeID, err)
		}
	}

	for _, nodeID := range []string{"node-a", "node-b"} {
		_, err := service.RecordP2PArtifact(context.Background(), &schedulerv1.RecordP2PArtifactRequest{
			ClusterId: "cluster-1",
			Backend:   "backend",
			Key:       "artifact-key",
			NodeId:    nodeID,
		})
		if err != nil {
			t.Fatalf("record artifact %s failed: %v", nodeID, err)
		}
	}
	_, err := service.RecordP2PArtifact(context.Background(), &schedulerv1.RecordP2PArtifactRequest{
		ClusterId: "cluster-1",
		Backend:   "other",
		Key:       "artifact-key",
		NodeId:    "node-c",
	})
	if err != nil {
		t.Fatalf("record other backend artifact failed: %v", err)
	}

	resp, err := service.LookupP2PArtifact(context.Background(), &schedulerv1.LookupP2PArtifactRequest{
		ClusterId:     "cluster-1",
		Backend:       "backend",
		Key:           "artifact-key",
		ExcludeNodeId: "node-a",
	})
	if err != nil {
		t.Fatalf("lookup artifact failed: %v", err)
	}
	if len(resp.GetPeers()) != 1 || resp.GetPeers()[0].GetNodeId() != "node-b" {
		t.Fatalf("expected only node-b, got %v", resp.GetPeers())
	}
	if got := resp.GetPeers()[0].GetEndpoint().GetAddress(); got != "addr-node-b" {
		t.Fatalf("expected node-b p2p address, got %q", got)
	}
}

func TestP2pArtifactForgetAndUnregisterRemoveProviders(t *testing.T) {
	registry := NewAtomicNodeRegistry([]Node{
		{ID: "node-a", Endpoint: "http://node-a"},
		{ID: "node-b", Endpoint: "http://node-b"},
	}, defaultObservedReportTTL)
	service := NewService(
		zap.NewNop(),
		registry,
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	for _, nodeID := range []string{"node-a", "node-b"} {
		_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
			NodeId:            nodeID,
			ClusterId:         "cluster-1",
			ServiceInstanceId: "svc-" + nodeID,
			Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
			P2PEndpoint:       &schedulerv1.P2PEndpoint{Backend: "backend", Address: "addr-" + nodeID},
		})
		if err != nil {
			t.Fatalf("heartbeat %s failed: %v", nodeID, err)
		}
		_, err = service.RecordP2PArtifact(context.Background(), &schedulerv1.RecordP2PArtifactRequest{
			ClusterId: "cluster-1",
			Backend:   "backend",
			Key:       "artifact-key",
			NodeId:    nodeID,
		})
		if err != nil {
			t.Fatalf("record artifact %s failed: %v", nodeID, err)
		}
	}

	_, err := service.ForgetP2PArtifact(context.Background(), &schedulerv1.ForgetP2PArtifactRequest{
		ClusterId: "cluster-1",
		Backend:   "backend",
		Key:       "artifact-key",
		NodeId:    "node-a",
	})
	if err != nil {
		t.Fatalf("forget artifact failed: %v", err)
	}
	resp, err := service.LookupP2PArtifact(context.Background(), &schedulerv1.LookupP2PArtifactRequest{
		ClusterId: "cluster-1",
		Backend:   "backend",
		Key:       "artifact-key",
	})
	if err != nil {
		t.Fatalf("lookup after forget failed: %v", err)
	}
	if len(resp.GetPeers()) != 1 || resp.GetPeers()[0].GetNodeId() != "node-b" {
		t.Fatalf("expected only node-b after forget, got %v", resp.GetPeers())
	}

	_, err = service.UnregisterNode(context.Background(), &schedulerv1.UnregisterNodeRequest{
		NodeId:            "node-b",
		ServiceInstanceId: "svc-node-b",
	})
	if err != nil {
		t.Fatalf("unregister failed: %v", err)
	}
	resp, err = service.LookupP2PArtifact(context.Background(), &schedulerv1.LookupP2PArtifactRequest{
		ClusterId: "cluster-1",
		Backend:   "backend",
		Key:       "artifact-key",
	})
	if err != nil {
		t.Fatalf("lookup after unregister failed: %v", err)
	}
	if len(resp.GetPeers()) != 0 {
		t.Fatalf("expected unregister to remove remaining artifact provider, got %v", resp.GetPeers())
	}
}

func TestP2pArtifactLookupExcludesExpiredNodes(t *testing.T) {
	shortTTL := 50 * time.Millisecond
	registry := NewAtomicNodeRegistry([]Node{
		{ID: "node-a", Endpoint: "http://node-a"},
		{ID: "node-b", Endpoint: "http://node-b"},
	}, shortTTL)
	service := NewService(
		zap.NewNop(),
		registry,
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	for _, nodeID := range []string{"node-a", "node-b"} {
		_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
			NodeId:            nodeID,
			ClusterId:         "cluster-1",
			ServiceInstanceId: "svc-" + nodeID,
			Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
			P2PEndpoint:       &schedulerv1.P2PEndpoint{Backend: "backend", Address: "addr-" + nodeID},
		})
		if err != nil {
			t.Fatalf("heartbeat %s failed: %v", nodeID, err)
		}
		_, err = service.RecordP2PArtifact(context.Background(), &schedulerv1.RecordP2PArtifactRequest{
			ClusterId: "cluster-1",
			Backend:   "backend",
			Key:       "artifact-key",
			NodeId:    nodeID,
		})
		if err != nil {
			t.Fatalf("record artifact %s failed: %v", nodeID, err)
		}
	}

	// Both nodes should be visible before TTL expires.
	resp, err := service.LookupP2PArtifact(context.Background(), &schedulerv1.LookupP2PArtifactRequest{
		ClusterId: "cluster-1",
		Backend:   "backend",
		Key:       "artifact-key",
	})
	if err != nil {
		t.Fatalf("lookup before expiry failed: %v", err)
	}
	if len(resp.GetPeers()) != 2 {
		t.Fatalf("expected 2 peers before TTL expiry, got %v", resp.GetPeers())
	}

	// Let node-a's heartbeat expire; refresh node-b.
	time.Sleep(shortTTL + 10*time.Millisecond)
	_, err = service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
		NodeId:            "node-b",
		ClusterId:         "cluster-1",
		ServiceInstanceId: "svc-node-b",
		Snapshot:          &schedulerv1.NodeSnapshot{Status: schedulerv1.NodeStatus_NODE_STATUS_READY},
		P2PEndpoint:       &schedulerv1.P2PEndpoint{Backend: "backend", Address: "addr-node-b"},
	})
	if err != nil {
		t.Fatalf("refresh heartbeat node-b failed: %v", err)
	}

	// node-a is now UNHEALTHY (TTL expired); only node-b should be returned.
	resp, err = service.LookupP2PArtifact(context.Background(), &schedulerv1.LookupP2PArtifactRequest{
		ClusterId: "cluster-1",
		Backend:   "backend",
		Key:       "artifact-key",
	})
	if err != nil {
		t.Fatalf("lookup after expiry failed: %v", err)
	}
	if len(resp.GetPeers()) != 1 || resp.GetPeers()[0].GetNodeId() != "node-b" {
		t.Fatalf("expected only node-b after node-a TTL expiry, got %v", resp.GetPeers())
	}
}

func TestUnregisterObservedNodeRejectsServiceInstanceMismatch(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	registerObservedNodeForTest(t, service, "node-a", "svc-a")

	_, err := service.UnregisterNode(context.Background(), &schedulerv1.UnregisterNodeRequest{
		NodeId:            "node-a",
		ServiceInstanceId: "svc-b",
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected failed precondition, got %v", err)
	}
}

func TestHeartbeatRejectsUnknownNode(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
		NodeId:            "node-unknown",
		ServiceInstanceId: "svc-x",
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("expected invalid argument, got %v", err)
	}
}

func TestHeartbeatRejectsEmptyNodeID(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
		NodeId:            "",
		ServiceInstanceId: "svc-a",
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("expected invalid argument, got %v", err)
	}
}

func TestHeartbeatRejectsEmptyServiceInstanceID(t *testing.T) {
	service := NewService(
		zap.NewNop(),
		NewAtomicNodeRegistry([]Node{{ID: "node-a", Endpoint: "http://node-a"}}, defaultObservedReportTTL),
		NewStrategy("round_robin"),
		NewInMemoryBindingStore(defaultObservedReportTTL),
	)

	_, err := service.Heartbeat(context.Background(), &schedulerv1.HeartbeatRequest{
		NodeId:            "node-a",
		ServiceInstanceId: "",
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("expected invalid argument, got %v", err)
	}
}
