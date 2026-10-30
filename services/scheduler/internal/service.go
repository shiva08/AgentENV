package scheduler

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	schedulerv1 "agentenv/services/api/proto"
	"agentenv/services/shared/config"

	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type Service struct {
	schedulerv1.UnimplementedSchedulerServer
	logger        *zap.Logger
	nodes         NodeRegistry
	strategy      Strategy
	store         BindingStore
	artifacts     ArtifactStore
	resourceLimit *config.NodeResourceLimit
	reserved      *reservations
	startedAt     time.Time
	startupGrace  time.Duration

	sandboxListsMu sync.Mutex
	sandboxLists   map[string]appliedSandboxList
	lossCheckedAt  time.Time
}

// appliedSandboxList is the last sandbox list the scheduler applied for a node.
type appliedSandboxList struct {
	fingerprint [sha256.Size]byte
	at          time.Time
}

// sandboxListResync re-applies an unchanged list now and then, so bindings lost from the store come back.
var sandboxListResync = 10 * time.Minute

// storeLossCheckInterval is how often a store that can lose its data is asked whether it did.
var storeLossCheckInterval = 5 * time.Second

// dataLossDetector is a binding store that can lose its data while the scheduler runs, such as Redis.
type dataLossDetector interface {
	DataLost() (bool, error)
}

func NewService(logger *zap.Logger, nodes NodeRegistry, strategy Strategy, store BindingStore, opts ...ServiceOption) *Service {
	if logger == nil {
		logger = zap.NewNop()
	}
	if nodes == nil {
		nodes = NewAtomicNodeRegistry(nil, defaultObservedReportTTL)
	}
	s := &Service{
		logger:       logger,
		nodes:        nodes,
		strategy:     strategy,
		store:        store,
		artifacts:    NewInMemoryArtifactStore(defaultArtifactStoreCapacity, 0),
		reserved:     newReservations(),
		startedAt:    time.Now(),
		sandboxLists: make(map[string]appliedSandboxList),
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// ServiceOption configures optional Service behaviour.
type ServiceOption func(*Service)

// WithNodeResourceLimit sets per-node resource thresholds for scheduling.
func WithNodeResourceLimit(limit *config.NodeResourceLimit) ServiceOption {
	return func(s *Service) {
		s.resourceLimit = limit
	}
}

func WithArtifactStore(store ArtifactStore) ServiceOption {
	return func(s *Service) {
		s.artifacts = store
	}
}

// WithStartupGrace lets nodes with no heartbeat yet get new sandboxes for d after
// the scheduler starts, since it has not listened long enough to miss one.
func WithStartupGrace(d time.Duration) ServiceOption {
	return func(s *Service) {
		s.startupGrace = d
	}
}

type QueryOnlyService struct {
	schedulerv1.UnimplementedSchedulerServer
	logger *zap.Logger
	store  BindingStore
}

func NewQueryOnlyService(logger *zap.Logger, store BindingStore) *QueryOnlyService {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &QueryOnlyService{logger: logger, store: store}
}

func (s *QueryOnlyService) LookupNode(_ context.Context, req *schedulerv1.LookupNodeRequest) (*schedulerv1.LookupNodeResponse, error) {
	return lookupNode(s.logger, s.store, nil, req)
}

func (s *Service) Schedule(_ context.Context, req *schedulerv1.ScheduleRequest) (resp *schedulerv1.ScheduleResponse, err error) {
	start := time.Now()
	defer func() {
		recordSchedulerSchedule(s.strategy.Name(), start, err)
	}()

	discovered := s.nodes.Snapshot( /* allowLingering */ false)
	excluded := make(map[string]bool, len(req.GetExcludeNodeIds()))
	for _, id := range req.GetExcludeNodeIds() {
		excluded[id] = true
	}
	now := time.Now()
	inStartupGrace := now.Sub(s.startedAt) < s.startupGrace
	ready := make([]Node, 0, len(discovered))
	for _, n := range discovered {
		if excluded[n.ID] {
			continue
		}
		observed, ok := s.nodes.GetObserved(n.ID, "", now)
		if (ok && observed.GetSnapshot().GetStatus() == schedulerv1.NodeStatus_NODE_STATUS_READY) || (!ok && inStartupGrace) {
			ready = append(ready, n)
		}
	}
	if len(discovered) > 0 && len(ready) == 0 {
		err = status.Error(codes.Unavailable, "no ready nodes")
		return nil, err
	}
	request := createUsage(req.GetHint())
	var pending map[string]usage
	if s.resourceLimit != nil {
		// From the capacity check to the reservation, so creates arriving together cannot take one free slot.
		s.reserved.mu.Lock()
		defer s.reserved.mu.Unlock()
		pending = s.reserved.pendingLocked(now)
	}
	rich := make([]RichNode, 0, len(ready))
	for _, n := range ready {
		rich = append(rich, RichNode{
			Node:     n,
			Snapshot: s.nodes.PeekObserved(n.ID),
		})
	}

	eligible := FilterByResourceLimit(rich, s.resourceLimit, pending, request)

	node, selectErr := s.strategy.Select(eligible, req.GetHint())
	if selectErr != nil {
		s.logger.Debug("scheduler selection failed",
			zap.String("strategy", s.strategy.Name()),
			zap.String("hint", summarizeScheduleHint(req.GetHint())),
			zap.Int("candidate_nodes", len(rich)),
			zap.Int("eligible_nodes", len(eligible)),
			zap.Error(selectErr),
		)
		if errors.Is(selectErr, ErrNoNodes) {
			if len(rich) > 0 {
				err = status.Error(codes.Unavailable, "no node has capacity")
				return nil, err
			}
			err = status.Error(codes.Unavailable, "no nodes available")
			return nil, err
		}
		err = status.Error(codes.Internal, selectErr.Error())
		return nil, err
	}
	resp = &schedulerv1.ScheduleResponse{Node: node.Node.ToProto()}
	if s.resourceLimit != nil && request.sandboxes > 0 {
		resp.ReservationId = s.reserved.addLocked(node.ID, request, now)
	}
	s.logger.Debug("scheduler selected node",
		zap.String("strategy", s.strategy.Name()),
		zap.String("hint", summarizeScheduleHint(req.GetHint())),
		zap.String("node_id", node.ID),
		zap.String("endpoint", node.Endpoint),
		zap.Int("candidate_nodes", len(rich)),
		zap.Int("eligible_nodes", len(eligible)),
		zap.String("reservation_id", resp.GetReservationId()),
	)
	return resp, nil
}

// summarizeScheduleHint renders a compact, log-friendly description of a
// scheduling hint.
func summarizeScheduleHint(hint *schedulerv1.ScheduleRequestHint) string {
	switch k := hint.GetKind().(type) {
	case *schedulerv1.ScheduleRequestHint_NewColdSandbox:
		c := k.NewColdSandbox
		return fmt.Sprintf("new_cold_sandbox cpu=%d memory_mb=%d images=%v", c.GetCpuCount(), c.GetMemoryMb(), c.GetImages())
	case *schedulerv1.ScheduleRequestHint_NewSandbox:
		return "new_sandbox"
	default:
		return "none"
	}
}

func (s *Service) ListNodes(_ context.Context, _ *schedulerv1.ListNodesRequest) (*schedulerv1.ListNodesResponse, error) {
	snapshot := s.nodes.Snapshot( /* allowLingering */ true)
	nodes := make([]*schedulerv1.Node, 0, len(snapshot))
	for _, node := range snapshot {
		nodes = append(nodes, node.ToProto())
	}

	s.logger.Debug("scheduler listed nodes", zap.Int("node_count", len(nodes)))

	return &schedulerv1.ListNodesResponse{Nodes: nodes}, nil
}

func (s *Service) LookupNode(_ context.Context, req *schedulerv1.LookupNodeRequest) (*schedulerv1.LookupNodeResponse, error) {
	return lookupNode(s.logger, s.store, s.nodes.Resolve, req)
}

// resolve, when set, supplies the node's current address. A node it does not know is
// reported unavailable: its saved address may now belong to another pod.
func lookupNode(logger *zap.Logger, store BindingStore, resolve func(string) (Node, bool), req *schedulerv1.LookupNodeRequest) (*schedulerv1.LookupNodeResponse, error) {
	if strings.TrimSpace(req.GetSandboxId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "sandbox_id is required")
	}
	node, ok, getErr := store.Get(req.GetSandboxId(), time.Now())
	if getErr != nil {
		logger.Warn("scheduler lookup binding store failed", zap.String("sandbox_id", req.GetSandboxId()), zap.Error(getErr))
		return nil, status.Error(codes.Unavailable, "binding store unavailable")
	}
	if !ok {
		logger.Debug("scheduler lookup missed sandbox assignment", zap.String("sandbox_id", req.GetSandboxId()))
		return nil, status.Error(codes.NotFound, "sandbox assignment not found")
	}
	if resolve != nil {
		current, found := resolve(node.ID)
		if !found || current.Endpoint == "" {
			logger.Debug("scheduler lookup found sandbox on an unknown node",
				zap.String("sandbox_id", req.GetSandboxId()),
				zap.String("node_id", node.ID),
			)
			return nil, status.Error(codes.Unavailable, "node unavailable")
		}
		node.Endpoint = current.Endpoint
	}
	logger.Debug("scheduler lookup resolved sandbox assignment",
		zap.String("sandbox_id", req.GetSandboxId()),
		zap.String("node_id", node.ID),
		zap.String("endpoint", node.Endpoint),
	)
	return &schedulerv1.LookupNodeResponse{Node: node.ToProto()}, nil
}

func (s *Service) RecordAssignment(_ context.Context, req *schedulerv1.RecordAssignmentRequest) (*schedulerv1.RecordAssignmentResponse, error) {
	if strings.TrimSpace(req.GetSandboxId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "sandbox_id is required")
	}
	node := NodeFromProto(req.GetNode())
	if strings.TrimSpace(node.ID) == "" || strings.TrimSpace(node.Endpoint) == "" {
		return nil, status.Error(codes.InvalidArgument, "node_id and endpoint are required")
	}
	if !s.isKnownNode(node) {
		s.logger.Warn("scheduler rejected assignment for unknown node",
			zap.String("sandbox_id", req.GetSandboxId()),
			zap.String("node_id", node.ID),
			zap.String("endpoint", node.Endpoint),
		)
		return nil, status.Error(codes.InvalidArgument, "node is not in scheduler node list")
	}
	if err := s.store.Record(req.GetSandboxId(), node, time.Now()); err != nil {
		s.logger.Warn("scheduler record assignment binding store failed",
			zap.String("sandbox_id", req.GetSandboxId()),
			zap.String("node_id", node.ID),
			zap.Error(err),
		)
		return nil, status.Error(codes.Unavailable, "binding store unavailable")
	}
	if id := req.GetReservationId(); id != "" {
		s.reserved.attach(id, req.GetSandboxId())
	}
	s.logger.Debug("scheduler recorded sandbox assignment",
		zap.String("sandbox_id", req.GetSandboxId()),
		zap.String("node_id", node.ID),
		zap.String("endpoint", node.Endpoint),
	)
	return &schedulerv1.RecordAssignmentResponse{}, nil
}

func (s *Service) ReleaseReservation(_ context.Context, req *schedulerv1.ReleaseReservationRequest) (*schedulerv1.ReleaseReservationResponse, error) {
	if strings.TrimSpace(req.GetReservationId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "reservation_id is required")
	}
	s.reserved.release(req.GetReservationId())
	return &schedulerv1.ReleaseReservationResponse{}, nil
}

func (s *Service) Heartbeat(_ context.Context, req *schedulerv1.HeartbeatRequest) (*schedulerv1.HeartbeatResponse, error) {
	nodeID := strings.TrimSpace(req.GetNodeId())
	serviceInstanceID := strings.TrimSpace(req.GetServiceInstanceId())
	if nodeID == "" || serviceInstanceID == "" {
		return nil, status.Error(codes.InvalidArgument, "node_id and service_instance_id are required")
	}

	now := time.Now()
	node, cpuConfigJSON, err := s.nodes.Heartbeat(req, now)
	if err != nil {
		if errors.Is(err, ErrNodeNotInRegistry) {
			s.logger.Warn("scheduler rejected observed registration for unknown node",
				zap.String("node_id", nodeID),
			)
			return nil, status.Error(codes.InvalidArgument, "node is not in scheduler node list")
		}
		return nil, status.Error(codes.Internal, "node registry heartbeat failed")
	}
	// The node's numbers now include the sandboxes it lists, so their reservations end.
	s.reserved.settle(nodeID, req.GetSandboxIds())
	// Every heartbeat carries the node's sandbox list; the store is touched only when it changed.
	fingerprint := sandboxListFingerprint(req.GetSandboxIds())
	s.forgetSandboxListsIfStoreLostData(now)
	if s.sandboxListUnchanged(nodeID, fingerprint, now) {
		recordSandboxList("unchanged")
		return &schedulerv1.HeartbeatResponse{CpuConfigJson: cpuConfigJSON}, nil
	}
	if err := s.store.ReconcileNode(node, req.GetSandboxIds(), now); err != nil {
		// Still answer OK: an error makes the node back off until it looks dead; the next heartbeat retries the write.
		s.logger.Warn("scheduler heartbeat binding reconcile failed",
			zap.String("node_id", nodeID),
			zap.Error(err),
		)
		return &schedulerv1.HeartbeatResponse{CpuConfigJson: cpuConfigJSON}, nil
	}
	s.sandboxListsMu.Lock()
	s.sandboxLists[nodeID] = appliedSandboxList{fingerprint: fingerprint, at: now}
	s.sandboxListsMu.Unlock()
	recordSandboxList("applied")
	s.logger.Debug("scheduler applied node sandbox list",
		zap.String("node_id", nodeID),
		zap.Int("sandbox_count", len(req.GetSandboxIds())),
	)
	return &schedulerv1.HeartbeatResponse{CpuConfigJson: cpuConfigJSON}, nil
}

func (s *Service) sandboxListUnchanged(nodeID string, fingerprint [sha256.Size]byte, now time.Time) bool {
	s.sandboxListsMu.Lock()
	defer s.sandboxListsMu.Unlock()
	last, ok := s.sandboxLists[nodeID]
	return ok && last.fingerprint == fingerprint && now.Sub(last.at) < sandboxListResync
}

// forgetSandboxListsIfStoreLostData makes every node's next list apply again once the store lost its bindings.
func (s *Service) forgetSandboxListsIfStoreLostData(now time.Time) {
	detector, ok := s.store.(dataLossDetector)
	if !ok {
		return
	}
	s.sandboxListsMu.Lock()
	due := now.Sub(s.lossCheckedAt) >= storeLossCheckInterval
	if due {
		s.lossCheckedAt = now
	}
	s.sandboxListsMu.Unlock()
	if !due {
		return
	}

	lost, err := detector.DataLost()
	if err != nil {
		s.logger.Warn("scheduler could not check the binding store for data loss", zap.Error(err))
		return
	}
	if !lost {
		return
	}
	s.sandboxListsMu.Lock()
	defer s.sandboxListsMu.Unlock()
	if len(s.sandboxLists) > 0 {
		s.logger.Warn("binding store lost its data; each node's next heartbeat rewrites its bindings")
		clear(s.sandboxLists)
	}
}

func sandboxListFingerprint(sandboxIDs []string) [sha256.Size]byte {
	ids := make([]string, 0, len(sandboxIDs))
	for _, id := range sandboxIDs {
		if id = strings.TrimSpace(id); id != "" {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return sha256.Sum256([]byte(strings.Join(ids, "\n")))
}

func (s *Service) ReportSandboxEvent(_ context.Context, req *schedulerv1.ReportSandboxEventRequest) (*schedulerv1.ReportSandboxEventResponse, error) {
	s.logger.Debug("scheduler ignored sandbox event batch",
		zap.String("node_id", req.GetNodeId()),
		zap.String("service_instance_id", req.GetServiceInstanceId()),
		zap.Int("event_count", len(req.GetEvents())),
	)
	return &schedulerv1.ReportSandboxEventResponse{}, nil
}

func (s *Service) RunObservedNodesMetrics(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = 15 * time.Second
	}
	s.refreshObservedNodesMetrics(time.Now())

	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			s.refreshObservedNodesMetrics(now)
		}
	}
}

func (s *Service) refreshObservedNodesMetrics(now time.Time) {
	recordObservedNodes(s.nodes.ListObserved("", now))
}

func (s *Service) ListObservedNodes(_ context.Context, req *schedulerv1.ListObservedNodesRequest) (*schedulerv1.ListObservedNodesResponse, error) {
	nodes := s.nodes.ListObserved(req.GetClusterId(), time.Now())
	return &schedulerv1.ListObservedNodesResponse{
		Nodes: nodes,
	}, nil
}

func (s *Service) ListP2PPeers(_ context.Context, req *schedulerv1.ListP2PPeersRequest) (*schedulerv1.ListP2PPeersResponse, error) {
	peers := s.nodes.ListP2pPeers(
		req.GetClusterId(),
		req.GetBackend(),
		req.GetExcludeNodeId(),
		time.Now(),
	)
	return &schedulerv1.ListP2PPeersResponse{Peers: peers}, nil
}

func (s *Service) RecordP2PArtifact(_ context.Context, req *schedulerv1.RecordP2PArtifactRequest) (*schedulerv1.RecordP2PArtifactResponse, error) {
	if strings.TrimSpace(req.GetClusterId()) == "" || strings.TrimSpace(req.GetBackend()) == "" || strings.TrimSpace(req.GetKey()) == "" || strings.TrimSpace(req.GetNodeId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "cluster_id, backend, key, and node_id are required")
	}
	if _, ok := s.nodes.Resolve(req.GetNodeId()); !ok {
		return nil, status.Error(codes.InvalidArgument, "node is not in scheduler node list")
	}

	s.artifacts.Record(req.GetClusterId(), req.GetBackend(), req.GetKey(), req.GetNodeId())
	s.logger.Debug("scheduler recorded P2P artifact",
		zap.String("cluster_id", req.GetClusterId()),
		zap.String("backend", req.GetBackend()),
		zap.String("key", req.GetKey()),
		zap.String("node_id", req.GetNodeId()),
	)
	return &schedulerv1.RecordP2PArtifactResponse{}, nil
}

func (s *Service) ForgetP2PArtifact(_ context.Context, req *schedulerv1.ForgetP2PArtifactRequest) (*schedulerv1.ForgetP2PArtifactResponse, error) {
	if strings.TrimSpace(req.GetClusterId()) == "" || strings.TrimSpace(req.GetBackend()) == "" || strings.TrimSpace(req.GetKey()) == "" || strings.TrimSpace(req.GetNodeId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "cluster_id, backend, key, and node_id are required")
	}

	s.artifacts.Forget(req.GetClusterId(), req.GetBackend(), req.GetKey(), req.GetNodeId())
	s.logger.Debug("scheduler forgot P2P artifact",
		zap.String("cluster_id", req.GetClusterId()),
		zap.String("backend", req.GetBackend()),
		zap.String("key", req.GetKey()),
		zap.String("node_id", req.GetNodeId()),
	)
	return &schedulerv1.ForgetP2PArtifactResponse{}, nil
}

func (s *Service) LookupP2PArtifact(_ context.Context, req *schedulerv1.LookupP2PArtifactRequest) (*schedulerv1.LookupP2PArtifactResponse, error) {
	if strings.TrimSpace(req.GetClusterId()) == "" || strings.TrimSpace(req.GetBackend()) == "" || strings.TrimSpace(req.GetKey()) == "" {
		return nil, status.Error(codes.InvalidArgument, "cluster_id, backend, and key are required")
	}

	nodeIDs := s.artifacts.Lookup(req.GetClusterId(), req.GetBackend(), req.GetKey())
	peers := s.nodes.FilterP2pPeers(
		req.GetClusterId(),
		req.GetBackend(),
		nodeIDs,
		req.GetExcludeNodeId(),
		time.Now(),
	)
	return &schedulerv1.LookupP2PArtifactResponse{Peers: peers}, nil
}

func (s *Service) GetNode(_ context.Context, req *schedulerv1.GetNodeRequest) (*schedulerv1.GetNodeResponse, error) {
	nodeID := strings.TrimSpace(req.GetNodeId())
	if nodeID == "" {
		return nil, status.Error(codes.InvalidArgument, "node_id is required")
	}

	node, ok := s.nodes.GetObserved(nodeID, req.GetClusterId(), time.Now())
	if !ok {
		return nil, status.Error(codes.NotFound, "observed node not found")
	}

	return &schedulerv1.GetNodeResponse{Node: node}, nil
}

func (s *Service) UnregisterNode(_ context.Context, req *schedulerv1.UnregisterNodeRequest) (*schedulerv1.UnregisterNodeResponse, error) {
	nodeID := strings.TrimSpace(req.GetNodeId())
	serviceInstanceID := strings.TrimSpace(req.GetServiceInstanceId())
	if nodeID == "" || serviceInstanceID == "" {
		return nil, status.Error(codes.InvalidArgument, "node_id and service_instance_id are required")
	}

	unregisterErr := s.nodes.UnregisterObserved(nodeID, serviceInstanceID)
	if unregisterErr != nil {
		if errors.Is(unregisterErr, ErrServiceInstanceMismatch) {
			return nil, status.Error(codes.FailedPrecondition, "service instance mismatch")
		}
		return nil, status.Error(codes.Internal, unregisterErr.Error())
	}

	// Bindings stay: a node that comes back under the same name still has its sandboxes,
	// and its next list removes the ones that are gone.
	s.sandboxListsMu.Lock()
	delete(s.sandboxLists, nodeID)
	s.sandboxListsMu.Unlock()
	s.artifacts.ForgetNode(nodeID)

	return &schedulerv1.UnregisterNodeResponse{}, nil
}

func (s *Service) isKnownNode(node Node) bool {
	return s.nodes.Contains(node)
}
