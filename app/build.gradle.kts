import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
}

val localProps = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use(::load)
}

fun localProp(name: String, default: String) = "\"" + (localProps.getProperty(name) ?: default) + "\""

android {
    namespace = "dev.tvlivesubs"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.tvlivesubs"
        minSdk = 29 // AudioPlaybackCapture
        targetSdk = 30 // matches the Mi Box (Android 11)
        versionCode = 1
        versionName = "0.1.0"

        // Overridable in local.properties (git-ignored).
        buildConfigField("String", "SERVER_URL", localProp("TVSUBS_SERVER_URL", "ws://192.168.1.147:8765"))
        buildConfigField("String", "SRC_LANG", localProp("TVSUBS_SRC_LANG", "auto"))
        buildConfigField("String", "DST_LANG", localProp("TVSUBS_DST_LANG", "ru"))
        // LOG_KEYS prints every key the accessibility service sees to logcat (tag Keys).
        buildConfigField("boolean", "LOG_KEYS", localProps.getProperty("TVSUBS_LOG_KEYS") ?: "false")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }

    lint {
        abortOnError = false
    }
}

dependencies {
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.okhttp)
}
