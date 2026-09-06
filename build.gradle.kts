import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm") version uncivLibs.versions.kotlin.get()
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
    testImplementation(uncivLibs.gdx)
    testImplementation(uncivLibs.junit)
}

tasks.test {
    workingDir = uncivAssets
    systemProperty("java.awt.headless", "true")
    maxParallelForks = 1
    inputs.property("uncivRevision", uncivRevision)
    inputs.dir(uncivAssets.resolve("jsons"))
        .withPropertyName("uncivRulesets")
        .withPathSensitivity(PathSensitivity.RELATIVE)
}

tasks.withType<JavaExec>().configureEach {
    workingDir = uncivAssets
    systemProperty("java.awt.headless", "true")
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
