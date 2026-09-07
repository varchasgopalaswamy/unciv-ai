pluginManagement {
    repositories {
        gradlePluginPortal()
        mavenCentral()
    }
}

rootProject.name = "unciv-ai"

val uncivOverride = providers.gradleProperty("uncivDir")
val isCi = providers.environmentVariable("CI")
    .map { it.equals("true", ignoreCase = true) || it == "1" }
    .getOrElse(false)
check(!isCi || !uncivOverride.isPresent) {
    "CI must use the pinned vendor/Unciv submodule; remove the uncivDir override."
}

val uncivRoot = file(uncivOverride.getOrElse("vendor/Unciv")).canonicalFile
check(uncivRoot.resolve("core/build.gradle.kts").isFile) {
    "Unciv core is missing at $uncivRoot. Run git submodule update --init --recursive " +
        "or select a valid checkout with -PuncivDir=/path/to/Unciv."
}
check(uncivRoot.resolve("android/assets/jsons").isDirectory) {
    "Unciv ruleset assets are missing from $uncivRoot. Code and assets must use the same checkout."
}
gradle.extensions.extraProperties["uncivRootDir"] = uncivRoot

dependencyResolutionManagement {
    repositories {
        mavenCentral()
        maven("https://jitpack.io") {
            content {
                includeGroup("com.github.MinnDevelopment")
                includeGroup("club.minnced")
            }
        }
    }
    versionCatalogs {
        create("uncivLibs") {
            from(files(uncivRoot.resolve("gradle/libs.versions.toml")))
        }
    }
}

includeBuild(uncivRoot) {
    name = "Unciv"
    dependencySubstitution {
        substitute(module("local.unciv:core")).using(project(":core"))
        substitute(module("local.unciv:desktop")).using(project(":desktop"))
    }
}
