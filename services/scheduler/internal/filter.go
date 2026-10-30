package scheduler

import "agentenv/services/shared/config"

// FilterByResourceLimit removes nodes that the request would take over a
// configured limit, counting capacity already reserved for other creates
// (pending, by node ID). Nodes without a heartbeat snapshot are always kept
// (they have no metrics to evaluate). A nil limit disables all filtering.
func FilterByResourceLimit(nodes []RichNode, limit *config.NodeResourceLimit, pending map[string]usage, request usage) []RichNode {
	if limit == nil {
		return nodes
	}

	result := make([]RichNode, 0, len(nodes))
	for _, n := range nodes {
		if n.Snapshot == nil {
			// No heartbeat yet — cannot evaluate limits; keep the node.
			result = append(result, n)
			continue
		}
		if !withinLimit(n, limit, pending[n.ID].plus(request)) {
			continue
		}
		result = append(result, n)
	}
	return result
}

func withinLimit(n RichNode, limit *config.NodeResourceLimit, extra usage) bool {
	s := n.Snapshot
	// A starting sandbox already holds its slot.
	sandboxes := s.GetSandboxCount() + s.GetSandboxStartingCount() + extra.sandboxes
	allocatedCPU := s.GetAllocatedCpu() + extra.cpu
	allocatedMemory := s.GetAllocatedMemoryBytes() + extra.memoryBytes

	if limit.MaxSandboxCount != nil && sandboxes > *limit.MaxSandboxCount {
		return false
	}
	if limit.MaxSandboxStartingCount != nil && s.GetSandboxStartingCount()+extra.sandboxes > *limit.MaxSandboxStartingCount {
		return false
	}
	if limit.MaxCPUUsedPercent != nil && s.GetCpuPercent() > *limit.MaxCPUUsedPercent {
		return false
	}
	if limit.MaxCPUAllocatedPercent != nil {
		if s.GetCpuCount() > 0 {
			allocatedPercent := allocatedCPU * 100 / s.GetCpuCount()
			if allocatedPercent > *limit.MaxCPUAllocatedPercent {
				return false
			}
		}
	}
	if limit.MaxMemoryUsedPercent != nil {
		if s.GetMemoryTotalBytes() > 0 {
			usedPercent := uint32(s.GetMemoryUsedBytes() * 100 / s.GetMemoryTotalBytes())
			if usedPercent > *limit.MaxMemoryUsedPercent {
				return false
			}
		}
	}
	if limit.MaxMemoryAllocatedPercent != nil {
		if s.GetMemoryTotalBytes() > 0 {
			allocatedPercent := uint32(allocatedMemory * 100 / s.GetMemoryTotalBytes())
			if allocatedPercent > *limit.MaxMemoryAllocatedPercent {
				return false
			}
		}
	}

	// "Including paused" ceilings sum the active running set with the paused
	// reservations reported in the snapshot. A node exceeding any of these is
	// dropped from scheduling candidates regardless of whether the active-only
	// counters are within limits.
	if limit.MaxSandboxCountIncludingPaused != nil {
		total := sandboxes + s.GetPausedSandboxCount()
		if total > *limit.MaxSandboxCountIncludingPaused {
			return false
		}
	}
	if limit.MaxAllocatedCPUIncludingPaused != nil {
		total := allocatedCPU + s.GetPausedAllocatedCpu()
		if total > *limit.MaxAllocatedCPUIncludingPaused {
			return false
		}
	}
	if limit.MaxAllocatedMemoryBytesIncludingPaused != nil {
		total := allocatedMemory + s.GetPausedAllocatedMemoryBytes()
		if total > *limit.MaxAllocatedMemoryBytesIncludingPaused {
			return false
		}
	}
	return true
}
