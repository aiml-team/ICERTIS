from pydantic import BaseModel
from typing import Optional, List


class ContractRecord(BaseModel):
    fileName: str
    sharePointPath: Optional[str] = None
    opportunityID: Optional[str] = None
    ae: Optional[str] = None
    legalEntity: Optional[str] = None
    customerName: Optional[str] = None
    agreementName: Optional[str] = None
    orderNumber: Optional[str] = None
    autoRenewalStatus: Optional[str] = None
    effectiveDate: Optional[str] = None
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    expiryDate: Optional[str] = None
    contractType: Optional[str] = None
    contractClassification: Optional[str] = None
    associatedMSAFileName: Optional[str] = None
    associatedNDAFileName: Optional[str] = None
    voidExclusionIndicator: Optional[str] = None
    extractionStatus: Optional[str] = None
    reviewRequired: Optional[bool] = None
    missingFields: Optional[str] = None
    processedDate: Optional[str] = None
    errorMessage: Optional[str] = None
    fileID: str
    migrate: Optional[bool] = None
    migrated: Optional[bool] = None
    migratedDate: Optional[str] = None
    runId: Optional[str] = None
    # ── New-schema fields (dbo.ContractInventory1) ─────────────────────
    id: Optional[int] = None
    folderName: Optional[str] = None
    folderType: Optional[str] = None
    batchID: Optional[str] = None
    batchNumber: Optional[int] = None
    pageCount: Optional[int] = None
    textCharacters: Optional[int] = None
    endCustomerName: Optional[str] = None
    quoteID: Optional[str] = None
    annualFeeIncrease: Optional[str] = None
    feeIncreaseDate: Optional[str] = None


class ContractsResponse(BaseModel):
    data: List[ContractRecord]
    total: int
