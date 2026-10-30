package scheduler

import (
	"crypto/rand"
	"sync"
	"time"

	schedulerv1 "agentenv/services/api/proto"
)

// reservationTTL ends a reservation nobody released or matched, such as one whose gateway crashed mid-create.
var reservationTTL = 10 * time.Minute

// usage is an amount of node capacity.
type usage struct {
	sandboxes   uint32
	cpu         uint32
	memoryBytes uint64
}

func (u usage) plus(o usage) usage {
	return usage{sandboxes: u.sandboxes + o.sandboxes, cpu: u.cpu + o.cpu, memoryBytes: u.memoryBytes + o.memoryBytes}
}

// createUsage is what a create asks for (only a slot when it names no size); other requests ask for nothing.
func createUsage(hint *schedulerv1.ScheduleRequestHint) usage {
	switch kind := hint.GetKind().(type) {
	case *schedulerv1.ScheduleRequestHint_NewColdSandbox:
		return usage{
			sandboxes:   1,
			cpu:         kind.NewColdSandbox.GetCpuCount(),
			memoryBytes: kind.NewColdSandbox.GetMemoryMb() * 1024 * 1024,
		}
	case *schedulerv1.ScheduleRequestHint_NewSandbox:
		return usage{sandboxes: 1}
	default:
		return usage{}
	}
}

type reservation struct {
	nodeID    string
	size      usage
	sandboxID string
	misses    int
	expiresAt time.Time
}

// reservations is capacity promised to creates the nodes' heartbeats may not show yet.
// Schedule holds mu from check to reserve; this is atomic only while one scheduler places sandboxes.
type reservations struct {
	mu   sync.Mutex
	byID map[string]*reservation
	// listed is the sandboxes each node's latest heartbeat listed.
	listed map[string]map[string]struct{}
}

func newReservations() *reservations {
	return &reservations{byID: make(map[string]*reservation), listed: make(map[string]map[string]struct{})}
}

// pendingLocked drops expired reservations and sums the rest by node. The caller holds mu.
func (r *reservations) pendingLocked(now time.Time) map[string]usage {
	pending := make(map[string]usage)
	for id, res := range r.byID {
		if now.After(res.expiresAt) {
			delete(r.byID, id)
			continue
		}
		pending[res.nodeID] = pending[res.nodeID].plus(res.size)
	}
	return pending
}

// addLocked reserves size on a node and returns the reservation's ID. The caller holds mu.
func (r *reservations) addLocked(nodeID string, size usage, now time.Time) string {
	id := rand.Text()
	r.byID[id] = &reservation{nodeID: nodeID, size: size, expiresAt: now.Add(reservationTTL)}
	return id
}

// release frees a reservation whose create made no sandbox.
func (r *reservations) release(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.byID, id)
}

// attach names the sandbox a reservation's create made, so the node's heartbeats can end it.
// A node lists a sandbox while creating it, so its latest heartbeat may already count it.
func (r *reservations) attach(id, sandboxID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	res, ok := r.byID[id]
	if !ok {
		return
	}
	if _, counted := r.listed[res.nodeID][sandboxID]; counted {
		delete(r.byID, id)
		return
	}
	res.sandboxID = sandboxID
}

// settle ends the reservations of sandboxes a node lists, since its heartbeat numbers count them now,
// and of sandboxes two heartbeats miss, since they are gone. One miss is not enough: a heartbeat
// built before the sandbox existed may arrive after its assignment.
func (r *reservations) settle(nodeID string, sandboxIDs []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	listed := make(map[string]struct{}, len(sandboxIDs))
	for _, sandboxID := range sandboxIDs {
		listed[sandboxID] = struct{}{}
	}
	r.listed[nodeID] = listed
	for id, res := range r.byID {
		if res.nodeID != nodeID || res.sandboxID == "" {
			continue
		}
		if _, ok := listed[res.sandboxID]; ok {
			delete(r.byID, id)
			continue
		}
		if res.misses++; res.misses >= 2 {
			delete(r.byID, id)
		}
	}
}
