param(
    [Parameter(Mandatory = $true)][string[]]$Paths,
    [Parameter(Mandatory = $true)][string]$PublisherName
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($PublisherName)) {
    throw 'Expected signing publisher must be nonempty.'
}
foreach ($artifact in $Paths) {
    if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
        throw "Signed artifact is missing: $artifact"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $artifact
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
        throw "Artifact does not have a valid Authenticode signature: $artifact ($($signature.Status))"
    }
    $publisher = $signature.SignerCertificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    if (-not [string]::Equals($publisher, $PublisherName, [StringComparison]::Ordinal)) {
        throw "Artifact signer does not match the expected publisher: $artifact"
    }
    if ($null -eq $signature.TimeStamperCertificate) {
        throw "Artifact signature has no timestamp: $artifact"
    }
    Write-Output "Verified Authenticode signature and timestamp: $artifact"
}
