package uncivai

/** A gameplay admission includes its economy partner; suspended leases use no compute slot. */
internal fun computingCapacityAvailable(
    jobs: Map<String, CommunicationJob>, id: String, civilization: String, purpose: String,
    parentJob: String?, now: Long, maxConcurrent: Int, maxPerCivilization: Int,
): Boolean {
    val active = jobs.filterValues { it.expires > now && it.computing }
    val reserved = active.filter { (id, job) -> job.purpose == "gameplay" &&
        jobs.values.none { it.parentJob == id && it.purpose == "economy" && it.expires > now } }
    val consumesReservation = purpose == "economy" && parentJob in reserved
    val needsPartner = purpose == "gameplay" && jobs.values.none {
        it.parentJob == id &&
            it.purpose == "economy" && it.expires > now
    }
    val needed = 1 + (if (needsPartner) 1 else 0) - (if (consumesReservation) 1 else 0)
    return active.size + reserved.size + needed <= maxConcurrent &&
        active.values.count { it.civilization == civilization } +
        reserved.values.count { it.civilization == civilization } + needed <= maxPerCivilization &&
        active.values.none { it.civilization == civilization && it.purpose == purpose }
}

/** Keep one invocation available for the first economy partner of an admitted main job. */
internal fun invocationCapacityAvailable(jobs: Map<String, CommunicationJob>, purpose: String,
    parentJob: String?, now: Long, invocations: Long, maximum: Long): Boolean {
    fun lacksPartner(id: String) = jobs.values.none { it.parentJob == id && it.purpose == "economy" }
    val reserved = jobs.count { (id, job) -> job.expires > now && job.purpose == "gameplay" && lacksPartner(id) }
    val consumes = purpose == "economy" && parentJob != null && jobs[parentJob]?.expires?.let { it > now } == true && lacksPartner(parentJob)
    return invocations.toLong() + reserved + (if (purpose == "gameplay") 2 else 1) - (if (consumes) 1 else 0) <= maximum
}
