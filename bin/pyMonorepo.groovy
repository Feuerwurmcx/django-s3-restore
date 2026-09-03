// vars/pyMonorepo.groovy
//
// Shared-Library-Steps für das Python-Monorepo.
//
// resources/ liegt NICHT auf dem Agent – libraryResource() liefert nur den
// Dateiinhalt als String. Die Skripte werden deshalb einmal pro Workspace
// materialisiert und danach ganz normal mit `bash <pfad>` aufgerufen.
//
// Aufruf im Jenkinsfile:
//     @Library('ci-shared@v1.0.0') _
//     pyMonorepo.install()
//     def pkgs = pyMonorepo.changedPackages(base)

// Bewusst Methoden statt statischer Felder: statische Felder in vars/-Skripten
// werden zwischen Builds geteilt und machen mit CPS/Serialisierung Ärger.
private String resourcePath() { return 'de/firma/ci' }
private List scripts() {
    return ['changed-packages.sh', 'version-of.sh', 'pack.sh', 'upload-nexus.sh']
}

/**
 * Schreibt die Skripte aus resources/ in den Workspace.
 * Einmal vor dem parallel-Block aufrufen, nicht in jedem Branch.
 *
 * @return Verzeichnis, in dem die Skripte liegen
 */
String install(String targetDir = '.ci-lib') {
    List names = scripts()
    names.each { name ->
        writeFile file: "${targetDir}/${name}",
                  text: libraryResource("${resourcePath()}/${name}"),
                  encoding: 'UTF-8'
    }
    // Kein chmod nötig: alle Aufrufe gehen über `bash <datei>`.
    // writeFile legt die Datei ohnehin ohne x-Bit an.
    echo "Skripte nach ${targetDir}/ geschrieben: ${names.join(', ')}"
    return targetDir
}

/** Geänderte Pakete seit base (leer = alle). */
List changedPackages(String base, String dir = '.ci-lib') {
    String out = sh(returnStdout: true,
                    script: "bash ${dir}/changed-packages.sh '${base}'").trim()
    return out ? out.split('\n') as List : []
}

/** Version eines Pakets aus setup.py / setup.cfg. */
String versionOf(String pkg, String dir = '.ci-lib') {
    return sh(returnStdout: true,
              script: "bash ${dir}/version-of.sh '${pkg}'").trim()
}

/** Packt ein Paket, gibt den Archivpfad zurück. */
String pack(String pkg, String version, String dir = '.ci-lib') {
    return sh(returnStdout: true,
              script: "bash ${dir}/pack.sh '${pkg}' '${version}'").trim()
}

/**
 * Lädt ein Archiv nach Nexus.
 *
 * pyMonorepo.publish(archive: a, pkg: p, version: v,
 *                    credentialsId: 'nexus-raw-deploy',
 *                    url: 'https://nexus...', repo: 'python-raw',
 *                    allowRedeploy: false)
 *
 * Das Secret wird NICHT in den Groovy-String interpoliert – withCredentials
 * legt es in die Umgebung, das Shell-Skript liest es selbst und gibt es curl
 * über stdin (nicht über argv).
 */
void publish(Map args) {
    String dir = args.get('dir', '.ci-lib')
    withEnv([
        "NEXUS_URL=${args.url}",
        "NEXUS_REPO=${args.repo}",
        "ALLOW_REDEPLOY=${args.get('allowRedeploy', false) ? '1' : '0'}"
    ]) {
        withCredentials([usernamePassword(
                credentialsId: args.credentialsId,
                usernameVariable: 'NEXUS_USER',
                passwordVariable: 'NEXUS_PASS')]) {
            sh "bash ${dir}/upload-nexus.sh '${args.archive}' '${args.pkg}' '${args.version}'"
        }
    }
}
