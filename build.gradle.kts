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
    implementation(uncivLibs.gdx)
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.11.0")
    implementation("io.modelcontextprotocol:kotlin-sdk-server:0.15.0")
    implementation(platform("io.ktor:ktor-bom:3.5.1"))
    implementation("io.ktor:ktor-server-core")
    implementation("io.ktor:ktor-server-cio")
    implementation("io.ktor:ktor-server-netty")
    testImplementation(uncivLibs.junit)
    testImplementation("io.modelcontextprotocol:kotlin-sdk-client:0.15.0")
    testImplementation("io.ktor:ktor-client-cio")
}

// Desktop dependencies are only used by the manual UI verification launcher.
val uiRuntime by configurations.creating {
    extendsFrom(configurations.testRuntimeClasspath.get())
}
dependencies {
    uiRuntime("local.unciv:desktop")
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

tasks.register<JavaExec>("prepareConquestFixture") {
    group = "verification"
    description = "Prepares a fresh checkpoint with legal captures immediately available, for live capture and victory verification."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.CombatRunProbe"
    args(
        providers.gradleProperty("combatFixtureDir")
            .orElse(layout.buildDirectory.dir("generated/conquest-fixture").map { it.asFile.absolutePath }).get(),
        "prepare-conquest",
    )
}

tasks.register<JavaExec>("prepareVictoryFixture") {
    group = "verification"
    description = "Prepares a fresh checkpoint one capital attack and disposition away from victory."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.CombatRunProbe"
    args(
        providers.gradleProperty("combatFixtureDir")
            .orElse(layout.buildDirectory.dir("generated/victory-fixture").map { it.asFile.absolutePath }).get(),
        "prepare-victory",
    )
}

tasks.register<JavaExec>("prepareEconomyFixture") {
    group = "verification"
    description = "Prepares a new economy exercise checkpoint without issuing player commands."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.EconomyRunProbe"
    args(
        providers.gradleProperty("economyFixtureDir")
            .orElse(layout.buildDirectory.dir("generated/economy-fixture").map { it.asFile.absolutePath }).get(),
        "prepare",
    )
}

tasks.register<JavaExec>("prepareCityManagementFixture") {
    group = "verification"
    description = "Prepares a peaceful two-city-per-player city management exercise with private launch credentials."
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "uncivai.CityManagementRunProbe"
    args(
        providers.gradleProperty("cityManagementFixtureDir")
            .orElse(layout.buildDirectory.dir("generated/city-management-fixture").map { it.asFile.absolutePath }).get(),
        "prepare",
    )
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

tasks.register("engineInfo") {
    group = "help"
    description = "Shows the engine checkout, revision and runtime assets selected for this build."
    doLast {
        logger.lifecycle("Unciv checkout: $uncivRoot")
        logger.lifecycle("Unciv revision: ${uncivRevision.get()}")
        logger.lifecycle("Unciv assets: $uncivAssets")
    }
}
