package uncivai

/** Suspended inference releases compute capacity while its job lease stays alive. */
internal fun computingJobPhase(phase: String) = phase !in setOf("WAITING_NEGOTIATION", "WAITING_ROLE", "ROLE_FINISHED", "COMPLETED", "FAILED", "INTERRUPTED")

private fun partners(military: Boolean) = if (military) setOf("economy", "military") else setOf("economy")

/** Reserve role partners only for an admitted, computing main job. */
internal fun computingCapacityAvailable(
    jobs: Map<String, CommunicationJob>,
    id: String,
    civilization: String,
    purpose: String,
    parentJob: String?,
    now: Long,
    maxConcurrent: Int,
    maxPerCivilization: Int,
    militaryRequired: Boolean = false,
): Boolean {
    val active = jobs.filterValues { it.expires > now && it.computing }
    val reserved = active.flatMap { (mainId, job) ->
        if (job.purpose != "gameplay") {
            emptyList()
        } else {
            partners(job.militaryPartnerRequired).filter { role ->
                jobs.values.none { it.parentJob == mainId && it.purpose == role && it.expires > now }
            }.map { Triple(mainId, job.civilization, it) }
        }
    }
    val consumes = reserved.any { it.first == parentJob && it.third == purpose }
    val ownPartners = if (purpose == "gameplay") {
        partners(militaryRequired).count { role ->
            jobs.values.none { it.parentJob == id && it.purpose == role && it.expires > now }
        }
    } else {
        0
    }
    val needed = 1 + ownPartners - if (consumes) 1 else 0
    return active.size + reserved.size + needed <= maxConcurrent &&
        active.values.count { it.civilization == civilization } + reserved.count { it.second == civilization } + needed <= maxPerCivilization &&
        active.values.none { it.civilization == civilization && it.purpose == purpose }
}

/** Role invocations are reserved without charging for a peacetime military conversation. */
internal fun invocationCapacityAvailable(
    jobs: Map<String, CommunicationJob>,
    purpose: String,
    parentJob: String?,
    now: Long,
    invocations: Long,
    maximum: Long,
    militaryRequired: Boolean = false,
): Boolean {
    val reserved = jobs.flatMap { (id, job) ->
        if (job.expires <= now || job.purpose != "gameplay") {
            emptyList()
        } else {
            partners(job.militaryPartnerRequired).filter { role ->
                jobs.values.none { it.parentJob == id && it.purpose == role }
            }.map { id to it }
        }
    }
    val consumes = reserved.any { it.first == parentJob && it.second == purpose }
    val needed = if (purpose == "gameplay") 1 + partners(militaryRequired).size else 1
    return invocations + reserved.size + needed - (if (consumes) 1 else 0) <= maximum
}
