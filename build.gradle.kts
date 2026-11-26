import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm") version uncivLibs.versions.kotlin.get()
    kotlin("plugin.serialization") version uncivLibs.versions.kotlin.get()
    application
    id("com.diffplug.spotless") version "8.10.2"
}

group = "local.uncivai"
version = "0.1.0-SNAPSHOT"

val uncivRoot = gradle.extensions.extraProperties["uncivRootDir"] as File
val uncivAssets = uncivRoot.resolve("android/assets")
val uncivRevision = providers.exec {
    workingDir(uncivRoot)
    commandLine("git", "rev-parse", "HEAD")
}.standardOutput.asText.map { it.trim() }

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}
kotlin {
    compilerOptions {
        jvmTarget = JvmTarget.JVM_21
    }
}

dependencies {
    implementation("local.unciv:core")
    implementation("local.unciv:desktop")
    implementation(uncivLibs.gdx)
    implementation(uncivLibs.gdx.backend.desktop)
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.11.0")
    implementation("com.github.ajalt.clikt:clikt:5.0.3")
    implementation("com.networknt:json-schema-validator:3.0.7")
    implementation("org.xerial:sqlite-jdbc:3.53.4.0")
    implementation("io.modelcontextprotocol:kotlin-sdk-server:0.15.0")
    implementation(platform("io.ktor:ktor-bom:3.5.1"))
    implementation("io.ktor:ktor-server-core")
    implementation("io.ktor:ktor-server-cio")
    implementation("io.ktor:ktor-server-netty")
    testImplementation(uncivLibs.junit)
    testImplementation("io.modelcontextprotocol:kotlin-sdk-client:0.15.0")
    testImplementation("io.ktor:ktor-client-cio")
}

// Inspection launchers use the same runtime as the authoritative desktop.
val uiRuntime by configurations.creating {
    extendsFrom(configurations.testRuntimeClasspath.get())
}

spotless {
    kotlin {
        target("src/**/*.kt")
        ktlint("1.8.0")
    }
    kotlinGradle {
        target("*.gradle.kts")
        ktlint("1.8.0")
    }
}

application {
    mainClass = "uncivai.MainKt"
    applicationDefaultJvmArgs = listOf(
        "-Djava.awt.headless=true",
        "-Duncivai.assetsDir=${uncivAssets.absolutePath}",
    )
}

tasks.test {
    workingDir = uncivAssets
    systemProperty("uncivai.testFixtures", project.file("tests/fixtures").absolutePath)
    systemProperty("java.awt.headless", "true")
    systemProperty("uncivai.assetsDir", uncivAssets.absolutePath)
    // Resolve the composite dependency only after project configuration is complete.
    doFirst {
        systemProperty("uncivai.testClasspath", classpath.asPath)
    }
    maxParallelForks = 1
    inputs.property("uncivRevision", uncivRevision)
    inputs.dir(uncivAssets.resolve("jsons"))
        .withPropertyName("uncivRulesets")
        .withPathSensitivity(PathSensitivity.RELATIVE)
}

tasks.register<JavaExec>("verifyHostReliability") {
    group = "verification"
    description = "Exercises concurrent accounting and health checks on a fresh game or a copy of a checkpoint."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.HostReliabilityProbe"
    workingDir = uncivAssets
    systemProperty("uncivai.assetsDir", uncivAssets.absolutePath)
    systemProperty("java.awt.headless", "true")
    maxHeapSize = "1g"
    doFirst {
        val output = providers.gradleProperty("reliabilityDir")
        require(output.isPresent) { "verifyHostReliability requires -PreliabilityDir=<new absolute directory>" }
        args(output.get())
        providers.gradleProperty("reliabilityCheckpoint").orNull?.let { args(it) }
    }
}

tasks.withType<JavaExec>().configureEach {
    workingDir = uncivAssets
    systemProperty("java.awt.headless", "true")
    systemProperty("uncivai.assetsDir", uncivAssets.absolutePath)
}

tasks.register<JavaExec>("generateS01Scenarios") {
    group = "verification"
    description = "Generates the two-Human-player saved scenarios used to verify headless loading."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.S01ScenarioGenerator"
    args(layout.buildDirectory.dir("generated/s01-scenarios").get().asFile.absolutePath)
}

tasks.register<JavaExec>("generateCityStateLiveFixture") {
    group = "verification"
    description = "Creates a fresh two-city-state checkpoint for guided live acceptance."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.CityStateFixtures"
    doFirst { args(providers.gradleProperty("cityStateLiveDir").get()) }
}

tasks.register<JavaExec>("generateAirLiveFixture") {
    group = "verification"
    description = "Creates a funded late-game checkpoint for air warfare acceptance."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.AirFixtures"
    doFirst { args(providers.gradleProperty("airLiveDir").get()) }
}

tasks.register<JavaExec>("generateNavalLiveFixture") {
    group = "verification"
    description = "Creates a coastal checkpoint for guided naval and destination-order acceptance."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.NavalFixtures"
    doFirst { args(providers.gradleProperty("navalLiveDir").get()) }
}

tasks.register<JavaExec>("generateSettlementLiveFixture") {
    group = "verification"
    description = "Creates a settler destination and scouting checkpoint for opt-in live acceptance."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.SettlementLiveFixture"
    doFirst { args(providers.gradleProperty("settlementLiveDir").get()) }
}

tasks.register<JavaExec>("prepareStrategyPositions") {
    group = "verification"
    description = "Validates saved strategic positions and creates isolated checkpoints for opt-in live evaluation."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.StrategyPositionFixtures"
    doFirst {
        args(
            providers.gradleProperty("strategyMode").getOrElse("prepare"),
            providers.gradleProperty("strategySource").getOrElse(project.file("tests/fixtures/strategy_positions").absolutePath),
            providers.gradleProperty("strategyOutput").get(),
            providers.gradleProperty("strategyCivilization").getOrElse(""),
        )
    }
}

tasks.register<JavaExec>("generateDominationPositions") {
    group = "verification"
    description = "Creates isolated early-game strategic choices with all native victory types enabled."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.DominationPositionFixtures"
    doFirst { args(providers.gradleProperty("dominationOutput").get()) }
}

tasks.register<JavaExec>("generateCampaignInitiativePositions") {
    group = "verification"
    description = "Creates native reconnaissance, siege and counteroffensive positions for isolated live evaluation."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.CampaignInitiativeFixtures"
    doFirst { args(providers.gradleProperty("campaignOutput").get()) }
}

tasks.register<JavaExec>("probeCampaignInitiative") {
    group = "verification"
    description = "Checks fixed native campaign sequences from isolated positions without model calls."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.CampaignInitiativeProbe"
    doFirst {
        args(providers.gradleProperty("campaignSource").get(), providers.gradleProperty("campaignProbeOutput").get())
    }
}

tasks.register<JavaExec>("evaluateMilitaryOpportunity") {
    group = "verification"
    description = "Compares native attack, reconnaissance and development from isolated saved positions without model calls."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.MilitaryOpportunityEvaluation"
    maxHeapSize = "2g"
    doFirst {
        systemProperty("uncivai.engineRevision", uncivRevision.get())
        args(
            providers.gradleProperty("opportunitySource").get(),
            providers.gradleProperty("opportunityOutput").get(),
            providers.gradleProperty("opportunityRounds").getOrElse("20"),
            providers.gradleProperty("opportunityVariants").getOrElse("base,reserves,strong-defense"),
            providers.gradleProperty("opportunityPolicies").getOrElse("attack,recon,develop"),
        )
    }
}

tasks.register<JavaExec>("searchBalancedMaps") {
    group = "verification"
    description = "Searches native Medium maps for comparable six-civilization openings with twelve city-states."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.MapBalanceSearch"
    maxHeapSize = "2g"
    doFirst {
        systemProperty("uncivai.engineRevision", uncivRevision.get())
        args(
            providers.gradleProperty("mapSearchDir").getOrElse(layout.buildDirectory.dir("map-balance-search").get().asFile.absolutePath),
            providers.gradleProperty("mapSearchCount").getOrElse("500"),
            providers.gradleProperty("mapSearchFirstSeed").getOrElse("1"),
            providers.gradleProperty("mapSearchPlacementSeed").getOrElse("20261027"),
        )
        val type = providers.gradleProperty("mapSearchType").orNull
        val placementSeed = providers.gradleProperty("mapSearchStartingPositionSeed").orNull
        val candidates = providers.gradleProperty("mapSearchCandidates").orNull
        if (candidates != null) {
            args(type ?: "both", placementSeed ?: "0", candidates)
        } else if (placementSeed != null) {
            args(type ?: "Pangaea", placementSeed)
        } else {
            type?.let { args(it) }
        }
    }
}

tasks.register<JavaExec>("assessBenchmarkStarts") {
    group = "verification"
    description = "Compares copied historical benchmark maps at their archived starting coordinates."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.BenchmarkStartAssessment"
    doFirst {
        args(providers.gradleProperty("benchmarkStartManifest").get(), providers.gradleProperty("benchmarkStartDir").get())
    }
}

tasks.register<JavaExec>("generateVictoryLiveFixtures") {
    group = "verification"
    description = "Creates fresh late-game checkpoints for opt-in live spaceship and election acceptance."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.VictoryLiveFixtures"
    doFirst {
        args(providers.gradleProperty("victoryLiveDir").get())
    }
}

tasks.register<JavaExec>("verifyTradeCycle") {
    group = "verification"
    description = "Drives the complete trade exercise over HTTPS without model calls and retains native checkpoints."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.TradeCycleProbe"
    args(providers.gradleProperty("tradeCycleDir").orElse(layout.buildDirectory.dir("generated/trade-cycle").map { it.asFile.absolutePath }).get())
}

tasks.register<JavaExec>("verifyWarScenario") {
    group = "verification"
    description = "Drives the prepared war exercises and retains settlement checkpoints without model calls."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.WarScenarioProbe"
    args(providers.gradleProperty("warScenarioDir").orElse(layout.buildDirectory.dir("generated/war-scenario").map { it.asFile.absolutePath }).get())
}

tasks.register<JavaExec>("verifyAgreementLifecycleScenario") {
    group = "verification"
    description = "Drives the live agreement fixture through native expiry, restart and a legal resource raid without model calls."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.AgreementLifecycleScenarioProbe"
    args(providers.gradleProperty("agreementScenarioDir").orElse(layout.buildDirectory.dir("generated/agreement-lifecycle-scenario").map { it.asFile.absolutePath }).get())
}

val s04UiScenarios = layout.buildDirectory.dir("generated/s04-ui")
val generateS04UiScenarios by tasks.registering(JavaExec::class) {
    group = "verification"
    description = "Generates saved games for manually comparing free great-person selection."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.S04UiScenarioGenerator"
    args(s04UiScenarios.get().asFile.absolutePath)
    outputs.dir(s04UiScenarios)
}

val uiVersion = providers.gradleProperty("uiVersion").getOrElse("after")
require(uiVersion in setOf("before", "after")) { "uiVersion must be before or after" }
val uiCase = providers.gradleProperty("uiCase").getOrElse("blocked-placement")
require(uiCase in setOf("ordinary-vanilla", "ordinary", "maya", "blocked-placement", "no-city", "empty-maya-pool")) {
    "Unknown uiCase: $uiCase"
}
val uiDirectory = layout.buildDirectory.dir("s04-ui/$uiVersion")
fun Sync.copyDesktopAssets(destination: Provider<Directory>) {
    into(destination)
    from(uncivRoot.resolve("android")) {
        into("android")
        include("assets/**", "Images*/**", "TexturePacker.settings")
        exclude("assets/mods/**", "assets/SaveFiles/**", "assets/MultiplayerFiles/**", "assets/GameSettings.json")
    }
    from(uncivRoot.resolve("docs")) { into("docs") }
}
tasks.register<Sync>("prepareDesktopAssets") {
    group = "application"
    description = "Copies the selected engine's desktop assets for a managed local game."
    copyDesktopAssets(layout.buildDirectory.dir("desktop-engine"))
}

tasks.register<JavaExec>("runManagedDesktop") {
    group = "verification"
    description = "Exercises native controls and authenticated players against one managed desktop game."
    dependsOn(tasks.testClasses, "prepareDesktopAssets")
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.ManagedDesktopProbe"
    systemProperty("uncivai.liveDesktop", providers.gradleProperty("managedLive").getOrElse("false"))
    systemProperty("uncivai.agentCli", project.file(".venv/bin/civ-agent").absolutePath)
    workingDir = layout.buildDirectory.dir("desktop-engine/android/assets").get().asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("managedDesktopDir")
        require(output.isPresent) { "runManagedDesktop requires -PmanagedDesktopDir=<new absolute directory>" }
        args(output.get())
    }
}
val prepareS04UiAssets by tasks.registering(Sync::class) {
    group = "verification"
    description = "Copies the selected engine's UI assets so desktop-generated files stay out of source."
    copyDesktopAssets(uiDirectory.map { it.dir("runtime-engine") })
}
tasks.register<JavaExec>("runS04Ui") {
    group = "verification"
    description = "Launches the real desktop UI and automatically loads an S04 scenario."
    dependsOn(generateS04UiScenarios, prepareS04UiAssets)
    classpath = sourceSets.test.get().output + uiRuntime
    mainClass = "uncivai.S04UiLauncher"
    workingDir = uiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    systemProperty("uncivai.uiSmokeTest", providers.gradleProperty("uiSmokeTest").getOrElse("false"))
    args(
        s04UiScenarios.get().file("$uiCase.json").asFile.absolutePath,
        uiDirectory.get().dir("data/$uiCase").asFile.absolutePath,
    )
}

val inspectionUiDirectory = layout.buildDirectory.dir("inspection-ui")
val prepareInspectionUiAssets by tasks.registering(Sync::class) {
    group = "verification"
    description = "Copies the selected engine's UI assets for inspecting exported saves."
    copyDesktopAssets(inspectionUiDirectory.map { it.dir("runtime-engine") })
}
tasks.register<JavaExec>("runInspectUi") {
    group = "verification"
    description = "Launches the real desktop UI and automatically loads an exported inspection save."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + uiRuntime
    mainClass = "uncivai.InspectionUiLauncher"
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    systemProperty("uncivai.inspectSmokeTest", providers.gradleProperty("inspectSmokeTest").getOrElse("false"))
    val savePath = providers.gradleProperty("inspectSave")
    val dataPath = providers.gradleProperty("inspectDataDir")
    doFirst {
        require(savePath.isPresent && dataPath.isPresent) {
            "runInspectUi requires -PinspectSave=<absolute save> and -PinspectDataDir=<absolute data directory>"
        }
        args(savePath.get(), dataPath.get())
    }
}

tasks.register<JavaExec>("verifyMixedDiplomacyScenario") {
    group = "verification"
    description = "Exercises mixed-controller diplomacy, native AI turns and pending-request recovery over HTTPS."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.MixedDiplomacyProbe"
    workingDir = uncivAssets
    systemProperty("uncivai.assetsDir", uncivAssets.absolutePath)
    args(providers.gradleProperty("mixedScenarioDir").orElse(layout.buildDirectory.dir("verification/mixed-diplomacy").map { it.asFile.absolutePath }).get())
}

tasks.register<JavaExec>("runMixedDiplomacyDesktop") {
    group = "verification"
    description = "Captures native demands, responses, denunciation and delayed AI offers through desktop controls."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + sourceSets.main.get().output + uiRuntime
    mainClass = "uncivai.MixedDiplomacyDesktopProbe"
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("mixedDesktopDir")
        require(output.isPresent) { "runMixedDiplomacyDesktop requires -PmixedDesktopDir=<new absolute directory>" }
        args(output.get())
    }
}

tasks.register<JavaExec>("runCityStateDesktop") {
    group = "verification"
    description = "Exercises rendered city-state diplomacy and merchant mission controls."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + sourceSets.main.get().output + uiRuntime
    mainClass = "uncivai.CityStateDesktopProbe"
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("cityStateDesktopDir")
        require(output.isPresent) { "runCityStateDesktop requires -PcityStateDesktopDir=<new absolute directory>" }
        args(output.get())
    }
}

tasks.register<JavaExec>("runNavalDesktop") {
    group = "verification"
    description = "Exercises rendered naval actions and persistent destination controls."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + sourceSets.main.get().output + uiRuntime
    mainClass = "uncivai.NavalDesktopProbe"
    environment("ALSOFT_DRIVERS", "null")
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("navalDesktopDir")
        require(output.isPresent) { "runNavalDesktop requires -PnavalDesktopDir=<new absolute directory>" }
        args(output.get())
    }
}

tasks.register<JavaExec>("runAirDesktop") {
    group = "verification"
    description = "Exercises rendered aircraft, paradrop and special combat controls."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + sourceSets.main.get().output + uiRuntime
    mainClass = "uncivai.AirDesktopProbe"
    environment("ALSOFT_DRIVERS", "null")
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("airDesktopDir")
        require(output.isPresent) { "runAirDesktop requires -PairDesktopDir=<new absolute directory>" }
        args(output.get())
    }
}

tasks.register<JavaExec>("runVictoryDesktop") {
    group = "verification"
    description = "Exercises rendered world-leader voting, results and spaceship assembly controls."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + sourceSets.main.get().output + uiRuntime
    mainClass = "uncivai.VictoryDesktopProbe"
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("victoryDesktopDir")
        require(output.isPresent) { "runVictoryDesktop requires -PvictoryDesktopDir=<new absolute directory>" }
        args(output.get())
    }
}

tasks.register<JavaExec>("runWarTradeDesktop") {
    group = "verification"
    description = "Captures real desktop war-trade screens and accepts native requests in isolated fixtures."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + sourceSets.main.get().output + uiRuntime
    mainClass = "uncivai.WarTradeDesktopProbe"
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("warDesktopDir")
        require(output.isPresent) { "runWarTradeDesktop requires -PwarDesktopDir=<new absolute directory>" }
        args(output.get())
    }
}

tasks.register<JavaExec>("runAgreementLifecycleDesktop") {
    group = "verification"
    description = "Compares player agreement records with rendered native trade rows and lifecycle notices."
    dependsOn(tasks.testClasses, prepareInspectionUiAssets)
    classpath = sourceSets.test.get().output + sourceSets.main.get().output + uiRuntime
    mainClass = "uncivai.AgreementLifecycleDesktopProbe"
    workingDir = inspectionUiDirectory.get().dir("runtime-engine/android/assets").asFile
    systemProperty("uncivai.assetsDir", workingDir.absolutePath)
    systemProperty("java.awt.headless", "false")
    doFirst {
        val output = providers.gradleProperty("agreementDesktopDir")
        require(output.isPresent) { "runAgreementLifecycleDesktop requires -PagreementDesktopDir=<new absolute directory>" }
        systemProperty("uncivai.engineRevision", uncivRevision.get())
        args(output.get())
    }
}

tasks.register("engineInfo") {
    group = "help"
    description = "Shows the engine checkout, revision and runtime assets selected for this build."
    doLast {
        logger.lifecycle("Unciv checkout: $uncivRoot")
        logger.lifecycle("Unciv revision: ${uncivRevision.get()}")
        logger.lifecycle("Unciv assets: $uncivAssets")
    }
}
