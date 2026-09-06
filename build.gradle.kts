import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm") version uncivLibs.versions.kotlin.get()
    application
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
    testImplementation(uncivLibs.junit)
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

tasks.register("engineInfo") {
    group = "help"
    description = "Shows the engine checkout, revision and runtime assets selected for this build."
    doLast {
        logger.lifecycle("Unciv checkout: $uncivRoot")
        logger.lifecycle("Unciv revision: ${uncivRevision.get()}")
        logger.lifecycle("Unciv assets: $uncivAssets")
    }
}
